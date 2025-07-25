import shutil

import pytest
import torch
from omegaconf import OmegaConf

from metatrain.pet import PET, Trainer
from metatrain.pet.modules.finetuning import (
    apply_finetuning_strategy,
)
from metatrain.utils.data import Dataset, DatasetInfo
from metatrain.utils.data.readers import read_systems, read_targets
from metatrain.utils.data.target_info import get_energy_target_info
from metatrain.utils.io import model_from_checkpoint

from . import DATASET_PATH, DEFAULT_HYPERS, MODEL_HYPERS


def test_lora_finetuning_functionality():
    target_info_dict = {}
    target_info_dict["energy"] = get_energy_target_info(
        {"quantity": "energy", "unit": "eV"}
    )
    dataset_info = DatasetInfo(
        length_unit="Angstrom", atomic_types=[1, 6, 7, 8], targets=target_info_dict
    )

    model = PET(MODEL_HYPERS, dataset_info)

    finetuning_strategy = {
        "method": "lora",
        "config": {
            "target_modules": ["input_linear", "output_linear"],
            "rank": 4,
            "alpha": 8,
        },
    }

    model = apply_finetuning_strategy(model, finetuning_strategy)
    num_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    num_params = sum(p.numel() for p in model.parameters())
    assert num_trainable_params < num_params


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_lora_finetuning_device(device):
    if device == "cuda" and not torch.cuda.is_available():
        pytest.skip("CUDA is not available")

    target_info_dict = {}
    target_info_dict["energy"] = get_energy_target_info(
        {"quantity": "energy", "unit": "eV"}
    )
    dataset_info = DatasetInfo(
        length_unit="Angstrom", atomic_types=[1, 6, 7, 8], targets=target_info_dict
    )

    model = PET(MODEL_HYPERS, dataset_info)

    finetuning_strategy = {
        "method": "lora",
        "config": {
            "target_modules": ["input_linear", "output_linear"],
            "rank": 4,
            "alpha": 8,
        },
    }

    model = apply_finetuning_strategy(model, finetuning_strategy)
    for param in model.parameters():
        assert param.device.type == device, f"Parameter {param.name} is not on {device}"


def test_heads_finetuning_functionality():
    target_info_dict = {}
    target_info_dict["energy"] = get_energy_target_info(
        {"quantity": "energy", "unit": "eV"}
    )
    dataset_info = DatasetInfo(
        length_unit="Angstrom", atomic_types=[1, 6, 7, 8], targets=target_info_dict
    )

    model = PET(MODEL_HYPERS, dataset_info)

    finetuning_strategy = {
        "method": "heads",
        "config": {
            "head_modules": ["input_linear", "output_linear"],
            "last_layer_modules": ["last_layers", "bond_last_layers"],
        },
    }

    model = apply_finetuning_strategy(model, finetuning_strategy)
    num_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    num_params = sum(p.numel() for p in model.parameters())
    assert num_trainable_params < num_params


def test_finetuning_restart(monkeypatch, tmp_path):
    """
    Test finetuning with LoRA layers.
    """

    monkeypatch.chdir(tmp_path)
    shutil.copy(DATASET_PATH, "qm9_reduced_100.xyz")

    systems = read_systems(DATASET_PATH)
    systems = [system.to(torch.float32) for system in systems]

    target_info_dict = {}
    target_info_dict["mtt::U0"] = get_energy_target_info(
        {"quantity": "energy", "unit": "eV"}
    )

    dataset_info = DatasetInfo(
        length_unit="Angstrom", atomic_types=[1, 6, 7, 8], targets=target_info_dict
    )
    model = PET(MODEL_HYPERS, dataset_info)

    conf = {
        "mtt::U0": {
            "quantity": "energy",
            "read_from": DATASET_PATH,
            "reader": "ase",
            "key": "U0",
            "unit": "eV",
            "type": "scalar",
            "per_atom": False,
            "num_subtargets": 1,
            "forces": False,
            "stress": False,
            "virial": False,
        }
    }
    targets, _ = read_targets(OmegaConf.create(conf))

    # systems in float64 are required for training
    systems = [system.to(torch.float64) for system in systems]

    dataset = Dataset.from_dict({"system": systems, "mtt::U0": targets["mtt::U0"]})

    hypers = DEFAULT_HYPERS.copy()

    hypers["training"]["num_epochs"] = 1

    # Pre-training
    trainer = Trainer(hypers["training"])
    trainer.train(
        model=model,
        dtype=torch.float32,
        devices=[torch.device("cpu")],
        train_datasets=[dataset],
        val_datasets=[dataset],
        checkpoint_dir=".",
    )
    trainer.save_checkpoint(model, "tmp.ckpt")

    # Finetuning
    model_finetune = model_from_checkpoint("tmp.ckpt", context="finetune")
    assert isinstance(model_finetune, PET)
    model_finetune.restart(dataset_info)

    hypers = DEFAULT_HYPERS.copy()

    hypers["training"]["num_epochs"] = 0

    hypers["training"]["finetune"] = {
        "method": "lora",
        "config": {
            "target_modules": ["input_linear", "output_linear"],
            "rank": 4,
            "alpha": 8,
        },
    }

    trainer = Trainer(hypers["training"])
    trainer.train(
        model=model_finetune,
        dtype=torch.float32,
        devices=[torch.device("cpu")],
        train_datasets=[dataset],
        val_datasets=[dataset],
        checkpoint_dir=".",
    )

    trainer.save_checkpoint(model_finetune, "finetuned.ckpt")

    assert any(["lora_" in name for name, _ in model_finetune.named_parameters()])

    # Finetuning restart
    model_finetune_restart = model_from_checkpoint("finetuned.ckpt", context="restart")
    assert isinstance(model_finetune_restart, PET)
    model_finetune_restart.restart(dataset_info)

    assert any(
        ["lora_" in name for name, _ in model_finetune_restart.named_parameters()]
    )

    hypers = DEFAULT_HYPERS.copy()

    hypers["training"]["num_epochs"] = 0

    hypers["training"]["finetune"] = {
        "method": "heads",
        "config": {
            "head_modules": ["input_linear", "output_linear"],
            "last_layer_modules": ["last_layers", "bond_last_layers"],
        },
    }

    trainer = Trainer(hypers["training"])
    trainer.train(
        model=model_finetune_restart,
        dtype=torch.float32,
        devices=[torch.device("cpu")],
        train_datasets=[dataset],
        val_datasets=[dataset],
        checkpoint_dir=".",
    )
