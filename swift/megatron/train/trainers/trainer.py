# Copyright (c) Alibaba, Inc. and its affiliates.

import time
from contextlib import contextmanager
from functools import partial

import torch
from megatron.core import mpu
from megatron.core.enums import ModelType
from megatron.core.num_microbatches_calculator import get_num_microbatches
from megatron.core.pipeline_parallel import get_forward_backward_func
from megatron.core.rerun_state_machine import RerunMode, get_rerun_state_machine
from megatron.core.utils import StragglerDetector
from megatron.training import ft_integration, get_args, get_timers, is_last_rank, pretrain, print_rank_0, training

from swift.utils import get_logger
from ..patcher import patch_megatron_data_collator
from ..utils import get_batch, get_swift_datasets_provider

logger = get_logger()


class MegatronTrainer:

    def __init__(self, args):
        self.args = args
        self.stimer = StragglerDetector()
        self._patch_megatron()

    @contextmanager
    def _get_iters(self, train_dataset, val_dataset):
        origin_initialize_megatron = training.initialize_megatron

        def initialize_megatron(*_args, **kwargs):
            res = origin_initialize_megatron(*_args, **kwargs)
            args = get_args()
            data_parallel_size = mpu.get_data_parallel_world_size()
            step_batch_size = args.micro_batch_size * data_parallel_size
            if args.train_iters is None:
                if hasattr(train_dataset, '__len__'):
                    dataset_sample = len(train_dataset) // step_batch_size * step_batch_size
                    args.train_iters = (dataset_sample * args.max_epochs // args.global_batch_size) + 1
                else:
                    raise ValueError(
                        'You are using a streaming training dataset. Please explicitly specify `--train_iters`.')
            if val_dataset is not None and args.eval_iters < 0:
                if hasattr(val_dataset, '__len__'):
                    dataset_sample = len(val_dataset) // step_batch_size * step_batch_size
                    args.eval_iters = max(dataset_sample // args.global_batch_size, 1)
                else:
                    raise ValueError(
                        'You are using a streaming validation dataset. Please explicitly specify `--eval_iters`.')
            return res

        training.initialize_megatron = initialize_megatron
        try:
            yield
        finally:
            training.initialize_megatron = origin_initialize_megatron

    @staticmethod
    def new_cyclic_iter(iter):
        args = get_args()
        max_epochs = args.max_epochs
        i = 0
        while True:
            if getattr(args, 'is_training', False):
                if max_epochs and i >= max_epochs:
                    logger.info(f'Training of {i} epochs has been completed, the training has finished.')
                    break
                logger.info(f'The training of Epoch {i} starts...')
            for x in iter:
                yield x
            i += 1

    @staticmethod
    @contextmanager
    def _training_context():
        args = get_args()
        args.is_training = True
        try:
            yield
        finally:
            args.is_training = False

    def _replace_data_iterator(self, data_iterator):
        return data_iterator

    def train_step(self, forward_step_func, data_iterator, model, optimizer, opt_param_scheduler, config):
        with self._training_context():
            try:
                data_iterator = self._replace_data_iterator(data_iterator)
                return self._origin_train_step(forward_step_func, data_iterator, model, optimizer, opt_param_scheduler,
                                               config)
            except StopIteration:
                return {}, True, True, True, 0, None, None

    def evaluate(self,
                 forward_step_func,
                 data_iterator,
                 model,
                 process_non_loss_data_func,
                 config,
                 verbose=False,
                 non_loss_data_func=None):
        """Evaluation."""
        args = get_args()
        timers = get_timers()

        timers('evaluate', log_level=0).start(barrier=True)

        if args.vision_pretraining and args.vision_pretraining_type == 'dino':
            from megatron.legacy.model.vision.knn_monitor import compute_feature_bank
            compute_feature_bank(model)

        # Turn on evaluation mode which disables dropout.
        for model_module in model:
            model_module.eval()

        # Disable result validation during evaluation
        rerun_state_machine = get_rerun_state_machine()
        rerun_mode = rerun_state_machine.get_mode()
        rerun_state_machine.set_mode(RerunMode.DISABLED)

        total_loss_dict = {}

        # make validation batch size independent from training batch size
        eval_batch_size = args.global_batch_size
        eval_num_microbatches = eval_batch_size // (args.micro_batch_size * args.data_parallel_size)

        with torch.no_grad():
            iteration = 0
            if verbose:
                print_rank_0(f'Evaluating on {args.eval_iters * eval_batch_size} samples')
            while iteration < args.eval_iters:
                iteration += 1
                if verbose:
                    print_rank_0(f'Evaluating iter {iteration}/{args.eval_iters}')

                forward_backward_func = get_forward_backward_func()
                # Don't care about timing during evaluation
                config.timers = None
                ft_integration.on_eval_step_start()
                data_iterator = self._replace_data_iterator(data_iterator)
                loss_dicts = forward_backward_func(
                    forward_step_func=forward_step_func,
                    data_iterator=data_iterator,
                    model=model,
                    num_microbatches=eval_num_microbatches,
                    seq_length=args.seq_length,
                    micro_batch_size=args.micro_batch_size,
                    decoder_seq_length=args.decoder_seq_length,
                    forward_only=True)
                ft_integration.on_eval_step_end()
                config.timers = get_timers()

                # Empty unused memory
                if args.empty_unused_memory_level >= 1:
                    torch.cuda.empty_cache()

                if mpu.is_pipeline_last_stage(ignore_virtual=True):
                    # Reduce across processes.
                    for loss_dict in loss_dicts:
                        for key in loss_dict:
                            if key not in total_loss_dict:
                                total_loss_dict[key] = torch.tensor([0.0, 0.0], dtype=torch.float).cuda()
                            val = loss_dict[key]
                            if isinstance(val, tuple) or isinstance(val, list):
                                total_loss_dict[key][0] += val[0]
                                total_loss_dict[key][1] += val[1]
                            else:
                                total_loss_dict[key][0] += val
                                total_loss_dict[key][1] += 1

                args.consumed_valid_samples += eval_batch_size

                if args.exit_duration_in_mins:
                    train_time = (time.time() - training._TRAIN_START_TIME) / 60.0
                    done_cuda = torch.tensor([train_time > args.exit_duration_in_mins], dtype=torch.int, device='cuda')
                    torch.distributed.all_reduce(done_cuda, op=torch.distributed.ReduceOp.MAX)
                    done = done_cuda.item()
                    if done:
                        rerun_state_machine.set_mode(rerun_mode)
                        print_rank_0('Exiting during evaluation, timelimit reached')
                        return None, None, True

            collected_non_loss_data = None
            if non_loss_data_func is not None:
                collected_non_loss_data = non_loss_data_func(model)
            elif process_non_loss_data_func is not None and is_last_rank():
                collected_non_loss_data = forward_backward_func(
                    forward_step_func=forward_step_func,
                    data_iterator=data_iterator,
                    model=model,
                    num_microbatches=get_num_microbatches(),
                    seq_length=args.seq_length,
                    micro_batch_size=args.micro_batch_size,
                    decoder_seq_length=args.decoder_seq_length,
                    forward_only=True,
                    collect_non_loss_data=True)

        # Move model back to the train mode.
        for model_module in model:
            model_module.train()

        for key in total_loss_dict:
            numerator, denominator = total_loss_dict[key]
            total_loss_dict[key] = numerator / denominator

        timers('evaluate').stop()
        timers.log(['evaluate'])

        rerun_state_machine.set_mode(rerun_mode)

        rerun_state_machine.set_mode(rerun_mode)

        return total_loss_dict, collected_non_loss_data, False

    def _patch_megatron(self):
        # support max_epochs
        self._origin_train_step = training.train_step
        training.train_step = self.train_step
        training.cyclic_iter = self.new_cyclic_iter
        # patch training_log
        self._origin_training_log = training.training_log
        # patch evaluate
        self._origin_evaluate = training.evaluate
        training.evaluate = self.evaluate

    def forward_step(self, data_iterator, model):
        from pretrain_gpt import loss_func

        timers = get_timers()

        # Get the batch.
        timers('batch-generator', log_level=2).start()
        with self.stimer(bdata=True):
            data = get_batch(data_iterator)
        if not data:
            raise StopIteration
        timers('batch-generator').stop()

        with self.stimer:
            output_tensor = model(**data)
        labels = data.get('labels')
        loss_mask = None if labels is None else (labels != -100).float()
        return output_tensor, partial(loss_func, loss_mask)

    def train(self, train_dataset, val_dataset, data_collator):
        args = self.args
        datasets_provider = get_swift_datasets_provider(train_dataset, val_dataset)
        datasets_provider.is_distributed = True
        with patch_megatron_data_collator(data_collator), self._get_iters(train_dataset, val_dataset):
            extra_args_provider = args.megatron_model_meta.extra_args_provider
            pretrain(
                datasets_provider,
                args.megatron_model_meta.model_provider,
                ModelType.encoder_or_decoder,
                self.forward_step,
                extra_args_provider=extra_args_provider,
                args_defaults=args.extra_args)
