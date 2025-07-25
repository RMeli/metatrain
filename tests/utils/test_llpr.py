import torch
from metatomic.torch import (
    AtomisticModel,
    ModelEvaluationOptions,
    ModelMetadata,
    ModelOutput,
)

from metatrain.utils.data import CollateFn, Dataset, read_systems, read_targets
from metatrain.utils.io import load_model
from metatrain.utils.llpr import LLPRUncertaintyModel
from metatrain.utils.neighbor_lists import (
    get_requested_neighbor_lists,
    get_system_with_neighbor_lists,
)

from . import RESOURCES_PATH


torch.manual_seed(42)


def test_llpr(tmpdir):
    model = load_model(str(RESOURCES_PATH / "model-64-bit.ckpt"))
    llpr_model = LLPRUncertaintyModel(model)

    qm9_systems = read_systems(RESOURCES_PATH / "qm9_reduced_100.xyz")
    target_config = {
        "energy": {
            "quantity": "energy",
            "read_from": str(RESOURCES_PATH / "qm9_reduced_100.xyz"),
            "reader": "ase",
            "key": "U0",
            "unit": "kcal/mol",
            "type": "scalar",
            "per_atom": False,
            "num_subtargets": 1,
            "forces": False,
            "stress": False,
            "virial": False,
        },
    }
    targets, _ = read_targets(target_config)
    requested_neighbor_lists = get_requested_neighbor_lists(llpr_model)
    qm9_systems = [
        get_system_with_neighbor_lists(system, requested_neighbor_lists)
        for system in qm9_systems
    ]
    dataset = Dataset.from_dict({"system": qm9_systems, **targets})
    collate_fn = CollateFn(target_keys=list(targets.keys()))
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=10,
        shuffle=False,
        collate_fn=collate_fn,
    )

    llpr_model.compute_covariance(dataloader)
    llpr_model.compute_inverse_covariance()

    exported_model = AtomisticModel(
        llpr_model.eval(),
        ModelMetadata(),
        llpr_model.capabilities,
    )

    evaluation_options = ModelEvaluationOptions(
        length_unit="angstrom",
        outputs={
            "energy_uncertainty": ModelOutput(per_atom=True),
            "energy": ModelOutput(per_atom=True),
            "mtt::aux::energy_last_layer_features": ModelOutput(per_atom=True),
        },
        selected_atoms=None,
    )

    outputs = exported_model(
        qm9_systems[:5], evaluation_options, check_consistency=True
    )

    assert "energy_uncertainty" in outputs
    assert "energy" in outputs
    assert "mtt::aux::energy_last_layer_features" in outputs

    assert outputs["energy_uncertainty"].block().samples.names == [
        "system",
        "atom",
    ]
    assert outputs["energy"].block().samples.names == ["system", "atom"]
    assert outputs["mtt::aux::energy_last_layer_features"].block().samples.names == [
        "system",
        "atom",
    ]

    # Now test the ensemble approach
    params = []  # One per element, SOAP-BPNN
    for name, param in llpr_model.model.named_parameters():
        if "last_layers" in name and "energy" in name:
            params.append(param.squeeze())
    weights = torch.cat(params)

    n_ensemble_members = 1000000  # converges slowly
    llpr_model.calibrate(dataloader)
    llpr_model.generate_ensemble({"energy": weights}, n_ensemble_members)
    assert "energy_ensemble" in llpr_model.capabilities.outputs

    with tmpdir.as_cwd():
        llpr_model.save_checkpoint("llpr_model.ckpt")
        llpr_model = load_model("llpr_model.ckpt")
        exported_model = llpr_model.export()
        exported_model.save(file="llpr_model.pt", collect_extensions="extensions")
        exported_model = load_model("llpr_model.pt", extensions_directory="extensions")

    evaluation_options = ModelEvaluationOptions(
        length_unit="angstrom",
        outputs={
            "energy": ModelOutput(per_atom=False),
            "energy_uncertainty": ModelOutput(per_atom=False),
            "energy_ensemble": ModelOutput(per_atom=False),
        },
        selected_atoms=None,
    )
    outputs = exported_model(
        qm9_systems[:5], evaluation_options, check_consistency=True
    )

    assert "energy_uncertainty" in outputs
    assert "energy_ensemble" in outputs

    analytical_uncertainty = outputs["energy_uncertainty"].block().values
    ensemble_uncertainty = torch.std(
        outputs["energy_ensemble"].block().values, dim=1, keepdim=True
    )

    torch.testing.assert_close(
        analytical_uncertainty, ensemble_uncertainty, rtol=5e-3, atol=0.0
    )
