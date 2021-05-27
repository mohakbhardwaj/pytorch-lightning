from typing import Any, Optional, Sequence, Union, Tuple, List, Dict
from collections import OrderedDict

from torch.utils.data.dataloader import DataLoader

from pytorch_lightning.core.step_result import Result
from pytorch_lightning.loops.base import Loop
from pytorch_lightning.loops.evaluation_loop import EvaluationLoop
from pytorch_lightning.trainer.states import TrainerFn
from pytorch_lightning.trainer.supporters import PredictionCollection
from pytorch_lightning.utilities.model_helpers import is_overridden
from pytorch_lightning.utilities.types import STEP_OUTPUT, EPOCH_OUTPUT


class EvaluationDataLoaderLoop(Loop):

    def __init__(self):
        super().__init__()
        self._dataloaders: Optional[Union[DataLoader, Sequence[DataLoader]]] = None
        self._max_batches: Optional[Union[int, Sequence[int]]] = None
        self.outputs = []
        self.evaluation_loop = EvaluationLoop()

    @property
    def current_dataloader_idx(self) -> int:
        return self.iteration_count

    @property
    def num_dataloaders(self):
        return self._get_num_dataloaders(self._dataloaders)

    def connect(self, trainer, *args, **kwargs):
        super().connect(trainer, *args, **kwargs)
        self.evaluation_loop.connect(trainer, *args, **kwargs)

    @property
    def done(self):
        return (self.current_dataloader_idx >= len(self._dataloaders)) or self.should_skip_evaluation(self._max_batches)

    def reset(self):
        self.iteration_count = 0

        # prepare dataloaders
        self._dataloaders, self._max_batches = self.get_evaluation_dataloaders()
        # bookkeeping
        self.outputs = []

        if isinstance(self._max_batches, int):
            self._max_batches = [self._max_batches] * len(self._dataloaders)

        self._max_batches = self._max_batches


    def advance(self, *args: Any, **kwargs: Any) -> None:
        dataloader = self._dataloaders[self.current_dataloader_idx]
        dataloader = self.trainer.accelerator.process_dataloader(dataloader)
        dl_max_batches = self._max_batches[self.current_dataloader_idx]

        dl_outputs = self.evaluation_loop.run(dataloader, self.current_dataloader_idx, dl_max_batches, self.num_dataloaders)

        # store batch level output per dataloader
        if self.should_track_batch_outputs_for_epoch_end:
            self.outputs.append(dl_outputs)

    def on_run_start(self, *args: Any, **kwargs: Any) -> None:
        # hook
        self.on_evaluation_start()
        self.on_evaluation_epoch_start()

    def on_run_end(self) -> Any:
        outputs = self.outputs

        # with a single dataloader don't pass a 2D list
        if len(outputs) > 0 and self.num_dataloaders == 1:
            outputs = outputs[0]

        # lightning module method
        self.evaluation_epoch_end(outputs)

        # hook
        self.on_evaluation_epoch_end()

        # log epoch metrics
        eval_loop_results = self.trainer.logger_connector.get_evaluate_epoch_results()

        # hook
        self.on_evaluation_end()

        return eval_loop_results



# HELPERS

    def get_evaluation_dataloaders(self) -> Tuple[Optional[List[DataLoader]], List[Union[int, float]]]:
        model = self.trainer.lightning_module

        # select dataloaders
        if self.trainer.testing:
            self.trainer.reset_test_dataloader(model)

            dataloaders = self.trainer.test_dataloaders
            max_batches = self.trainer.num_test_batches
        else:
            # val
            if self.trainer.val_dataloaders is None or self.trainer.reload_dataloaders_every_epoch:
                self.trainer.reset_val_dataloader(model)
            if self.trainer.sanity_checking:
                self.trainer.num_sanity_val_batches = [
                    min(self.trainer.num_sanity_val_steps, val_batches) for val_batches in self.trainer.num_val_batches
                ]
                max_batches = self.trainer.num_sanity_val_batches
            else:
                max_batches = self.trainer.num_val_batches
            dataloaders = self.trainer.val_dataloaders
        return dataloaders, max_batches

    def should_skip_evaluation(self, max_batches: List[Union[int, float]]) -> bool:
        return sum(max_batches) == 0

    def on_evaluation_start(self, *args: Any, **kwargs: Any) -> None:
        self.should_track_batch_outputs_for_epoch_end: bool = self._should_track_batch_outputs_for_epoch_end()
        if self.trainer.testing:
            self.trainer.call_hook('on_test_start', *args, **kwargs)
        else:
            self.trainer.call_hook('on_validation_start', *args, **kwargs)

    def on_evaluation_model_eval(self) -> None:
        model_ref = self.trainer.lightning_module
        if self.trainer.testing:
            model_ref.on_test_model_eval()
        else:
            model_ref.on_validation_model_eval()

    def on_evaluation_model_train(self) -> None:
        model_ref = self.trainer.lightning_module
        if self.trainer.testing:
            model_ref.on_test_model_train()
        else:
            model_ref.on_validation_model_train()

    def on_evaluation_end(self, *args: Any, **kwargs: Any) -> None:
        if self.trainer.testing:
            self.trainer.call_hook('on_test_end', *args, **kwargs)
        else:
            self.trainer.call_hook('on_validation_end', *args, **kwargs)

        if self.trainer.state.fn != TrainerFn.FITTING:
            # summarize profile results
            self.trainer.profiler.describe()

    def reload_evaluation_dataloaders(self) -> None:
        model = self.trainer.lightning_module
        if self.trainer.testing:
            self.trainer.reset_test_dataloader(model)
        else:
            self.trainer.reset_val_dataloader(model)

    def on_evaluation_epoch_start(self, *args: Any, **kwargs: Any) -> None:
        self.trainer.call_hook('on_epoch_start', *args, **kwargs)

        if self.trainer.testing:
            self.trainer.call_hook('on_test_epoch_start', *args, **kwargs)
        else:
            self.trainer.call_hook('on_validation_epoch_start', *args, **kwargs)

    def _get_num_dataloaders(self, dataloaders: Optional[List[DataLoader]]) -> int:
        # case where user does:
        # return dl1, dl2
        if dataloaders is not None:
            length = len(dataloaders)
            if len(dataloaders) > 0 and isinstance(dataloaders[0], (list, tuple)):
                length = len(dataloaders[0])
            return length
        else:
            return 0

    def _should_track_batch_outputs_for_epoch_end(self) -> bool:
        model = self.trainer.lightning_module
        if self.trainer.testing:
            return is_overridden('test_epoch_end', model=model)
        else:
            return is_overridden('validation_epoch_end', model=model)

    def evaluation_epoch_end(self, outputs: EPOCH_OUTPUT) -> None:
        # unset dataloder_idx in model
        self.trainer.logger_connector.evaluation_epoch_end()

        # call the model epoch end
        model = self.trainer.lightning_module

        if self.trainer.testing:
            if is_overridden('test_epoch_end', model=model):
                model._current_fx_name = 'test_epoch_end'
                model.test_epoch_end(outputs)

        else:
            if is_overridden('validation_epoch_end', model=model):
                model._current_fx_name = 'validation_epoch_end'
                model.validation_epoch_end(outputs)

        # capture logging
        self.trainer.logger_connector.cache_logged_metrics()

    def store_predictions(self, output: Optional[STEP_OUTPUT], batch_idx: int, dataloader_idx: int) -> None:
        # Add step predictions to prediction collection to write later
        if output is not None and self.predictions is not None:
            if isinstance(output, Result) and self.trainer.testing:
                self.predictions.add(output.pop('predictions', None))

        # track debug metrics
        self.trainer.dev_debugger.track_eval_loss_history(batch_idx, dataloader_idx, output)

    def on_evaluation_epoch_end(self) -> None:
        hook_name = "on_test_epoch_end" if self.trainer.testing else "on_validation_epoch_end"
        self.trainer.call_hook(hook_name)
        self.trainer.call_hook('on_epoch_end')
