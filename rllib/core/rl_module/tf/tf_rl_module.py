import pathlib
from typing import Any, Mapping, Union

from ray.rllib.core.rl_module import RLModule
from ray.rllib.utils.annotations import override
from ray.rllib.utils.framework import try_import_tf


_, tf, _ = try_import_tf()


class TfRLModule(RLModule, tf.keras.Model):
    """Base class for RLlib TF RLModules."""

    def __init__(self, *args, **kwargs) -> None:
        tf.keras.Model.__init__(self)
        RLModule.__init__(self, *args, **kwargs)

    def call(self, batch: Mapping[str, Any], **kwargs) -> Mapping[str, Any]:
        """Forward pass of the module.

        Note:
            This is aliased to forward_train to follow the Keras Model API.

        Args:
            batch: The input batch. This input batch should comply with
                input_specs_train().
            **kwargs: Additional keyword arguments.

        Returns:
            The output of the forward pass. This output should comply with the
            ouptut_specs_train().

        """
        return self.forward_train(batch)

    @override(RLModule)
    def get_state(self) -> Mapping[str, Any]:
        return self.get_weights()

    @override(RLModule)
    def set_state(self, state_dict: Mapping[str, Any]) -> None:
        self.set_weights(state_dict)

    @override(RLModule)
    def _module_state_file_name(self) -> pathlib.Path:
        # TF checkpointing in the native tf format saves the weights as multiple
        # files, and when calling save_weights, the name passed should have no
        # file ending (e.g. .h5).
        return pathlib.Path("module_state")

    @override(RLModule)
    def save_state_to_file(self, path: Union[str, pathlib.Path]) -> str:
        """Saves the weights of this RLmodule to path.

        Args:
            path: The file path to save the checkpoint to.

        NOTE: For this TfRLModule, we save the weights in the TF checkpoint
            format, so the file name should have no ending and should be a plain string.
            e.g. "my_checkpoint" instead of "my_checkpoint.h5". This method of
            checkpointing saves the module weights as multiple files, so we recommend
            passing a file path relative to a directory, e.g.
            "my_checkpoint/module_state".

        Returns:
            The path to the saved checkpoint.
        """
        self.save_weights(path, save_format="tf")

    @override(RLModule)
    def load_state_from_file(self, path: Union[str, pathlib.Path]) -> None:
        self.load_weights(str(path))

    @override(RLModule)
    def make_distributed(self, dist_config: Mapping[str, Any] = None) -> None:
        """Makes the module distributed."""
        # TODO (Avnish): Implement this.
        pass

    @override(RLModule)
    def is_distributed(self) -> bool:
        """Returns True if the module is distributed."""
        # TODO (Avnish): Implement this.
        return False
