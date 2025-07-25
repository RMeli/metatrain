import re
import warnings
from pathlib import Path
from typing import Any, Dict, Literal, Optional, Union
from urllib.parse import unquote, urlparse
from urllib.request import urlretrieve

import torch
from metatomic.torch import check_atomistic_model, load_atomistic_model

from ..utils.architectures import find_all_architectures
from .architectures import import_architecture


def check_file_extension(
    filename: Union[str, Path], extension: str
) -> Union[str, Path]:
    """Check the file extension of a file name and adds if it is not present.

    If ``filename`` does not end with ``extension`` the ``extension`` is added and a
    warning will be issued.

    :param filename: Name of the file to be checked.
    :param extension: Expected file extension i.e. ``.txt``.
    :returns: Checked and probably extended file name.
    """
    path_filename = Path(filename)

    if path_filename.suffix != extension:
        warnings.warn(
            f"The file name should have a '{extension}' file extension. The user "
            f"requested the file with name '{filename}', but it will be saved as "
            f"'{filename}{extension}'.",
            stacklevel=1,
        )
        path_filename = path_filename.parent / (path_filename.name + extension)

    if type(filename) is str:
        return str(path_filename)
    else:
        return path_filename


def is_exported_file(path: str) -> bool:
    """
    Check if a saved model file has been exported to a metatomic ``AtomisticModel``.

    The functions uses :py:func:`metatomic.torch.check_atomistic_model` to verify.

    :param path: model path
    :return: :py:obj:`True` if the ``model`` has been exported, :py:obj:`False`
        otherwise.

    .. seealso::

        :py:func:`metatomic.torch.is_atomistic_model` to verify if an already loaded
        model is exported.
    """
    try:
        check_atomistic_model(str(path))
        return True
    except ValueError:
        return False


def _hf_hub_download_url(url: str, hf_token: Optional[str] = None) -> str:
    """Wrapper around `hf_hub_download` allowing passing the URL directly.

    Function is in inverse of `hf_hub_url`
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise ImportError(
            "To download a private model please install the `huggingface_hub` package "
            "with pip (`pip install huggingface_hub`)."
        )

    pattern = re.compile(
        r"(?P<endpoint>https://[^/]+)/"
        r"(?P<repo_id>[^/]+/[^/]+)/"
        r"resolve/"
        r"(?P<revision>[^/]+)/"
        r"(?P<filename>.+)"
    )

    match = pattern.match(url)

    if not match:
        raise ValueError(f"URL '{url}' has an invalid format for the Hugging Face Hub.")

    endpoint = match.group("endpoint")
    repo_id = match.group("repo_id")
    revision = unquote(match.group("revision"))
    filename = unquote(match.group("filename"))

    # Extract subfolder if applicable
    parts = filename.split("/", 1)
    if len(parts) == 2:
        subfolder, filename = parts
    else:
        subfolder = None

    return hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        subfolder=subfolder,
        repo_type=None,
        revision=revision,
        endpoint=endpoint,
        token=hf_token,
    )


def load_model(
    path: Union[str, Path],
    extensions_directory: Optional[Union[str, Path]] = None,
    hf_token: Optional[str] = None,
) -> Any:
    """Load checkpoints and exported models from an URL or a local file for inference.

    If an exported model should be loaded and requires compiled extensions, their
    location should be passed using the ``extensions_directory`` parameter.

    After reading a checkpoint, the returned model can be exported with the model's own
    ``export()`` method.

    .. note::

        This function is intended to load models for inference in Python. For continue
        training or finetuning use metatrain's command line interfaace

    :param path: local or remote path to a model. For supported URL schemes see
        :py:class:`urllib.request`
    :param extensions_directory: path to a directory containing all extensions required
        by an *exported* model
    :param hf_token: HuggingFace API token to download (private) models from HuggingFace

    :raises ValueError: if ``path`` is a YAML option file and no model
    :raises ValueError: if no ``archietcture_name`` is found in the checkpoint
    :raises ValueError: if the ``architecture_name`` is not found in the available
        architectures
    """

    if Path(path).suffix in [".yaml", ".yml"]:
        raise ValueError(
            f"path '{path}' seems to be a YAML option file and not a model"
        )

    path = str(path)

    # Download remote model
    # TODO(@PicoCentauri): Introduce caching for remote models
    if urlparse(path).scheme:
        if hf_token is None:
            path, _ = urlretrieve(path)
        else:
            path = _hf_hub_download_url(path, hf_token=hf_token)

    if is_exported_file(path):
        return load_atomistic_model(path, extensions_directory=extensions_directory)
    else:  # model is a checkpoint
        return model_from_checkpoint(path, context="export")


def model_from_checkpoint(
    path: Union[str, Path],
    context: Literal["restart", "finetune", "export"],
) -> torch.nn.Module:
    """
    Load the checkpoint at the given ``path``, and create the corresponding model
    instance. The model architecture is determined from information stored inside the
    checkpoint.
    """
    checkpoint = torch.load(path, weights_only=False, map_location="cpu")

    architecture_name = checkpoint["architecture_name"]
    if architecture_name not in find_all_architectures():
        raise ValueError(
            f"Checkpoint architecture '{architecture_name}' not found "
            "in the available architectures. Available architectures are: "
            f"{find_all_architectures()}"
        )
    architecture = import_architecture(architecture_name)

    model_ckpt_version = checkpoint.get("model_ckpt_version")
    ckpt_before_versionning = model_ckpt_version is None
    if ckpt_before_versionning:
        # assume version 1 and try our best
        model_ckpt_version = 1
        checkpoint["model_ckpt_version"] = model_ckpt_version

    if model_ckpt_version != architecture.__model__.__checkpoint_version__:
        try:
            if ckpt_before_versionning:
                warnings.warn(
                    "trying to upgrade an old model checkpoint with unknown "
                    "version, this might fail and require manual modifications",
                    stacklevel=1,
                )

            checkpoint = architecture.__model__.upgrade_checkpoint(checkpoint)
        except Exception as e:
            raise RuntimeError(
                f"Unable to load the model checkpoint from '{path}' for "
                f"the '{architecture_name}' architecture: the checkpoint is using "
                f"version {model_ckpt_version}, while the current version is "
                f"{architecture.__model__.__checkpoint_version__}; and trying to "
                "upgrade the checkpoint failed."
            ) from e

    try:
        return architecture.__model__.load_checkpoint(checkpoint, context=context)
    except Exception as e:
        raise ValueError(
            f"the file at '{path}' does not contain a valid checkpoint for "
            f"the '{architecture_name}' architecture"
        ) from e


def trainer_from_checkpoint(
    path: Union[str, Path],
    context: Literal["restart", "finetune", "export"],
    hypers: Dict[str, Any],
) -> Any:
    """
    Load the checkpoint at the given ``path``, and create the corresponding trainer
    instance. The architecture is determined from information stored inside the
    checkpoint.
    """
    checkpoint = torch.load(path, weights_only=False, map_location="cpu")

    architecture_name = checkpoint["architecture_name"]
    if architecture_name not in find_all_architectures():
        raise ValueError(
            f"Checkpoint architecture '{architecture_name}' not found "
            "in the available architectures. Available architectures are: "
            f"{find_all_architectures()}"
        )
    architecture = import_architecture(architecture_name)

    trainer_ckpt_version = checkpoint.get("trainer_ckpt_version")
    ckpt_before_versionning = trainer_ckpt_version is None
    if ckpt_before_versionning:
        # assume version 1 and try our best
        trainer_ckpt_version = 1
        checkpoint["trainer_ckpt_version"] = trainer_ckpt_version

    if trainer_ckpt_version != architecture.__trainer__.__checkpoint_version__:
        try:
            if ckpt_before_versionning:
                warnings.warn(
                    "trying to upgrade an old trainer checkpoint with unknown "
                    "version, this might fail and require manual modifications",
                    stacklevel=1,
                )

            checkpoint = architecture.__trainer__.upgrade_checkpoint(checkpoint)
        except Exception as e:
            raise RuntimeError(
                f"Unable to load the trainer checkpoint from '{path}' for "
                f"the '{architecture_name}' architecture: the checkpoint is using "
                f"version {trainer_ckpt_version}, while the current version is "
                f"{architecture.__trainer__.__checkpoint_version__}; and trying to "
                "upgrade the checkpoint failed."
            ) from e

    try:
        return architecture.__trainer__.load_checkpoint(
            checkpoint, context=context, hypers=hypers
        )
    except Exception as err:
        raise ValueError(
            f"path '{path}' is not a valid checkpoint for the {architecture_name} "
            "trainer state"
        ) from err
