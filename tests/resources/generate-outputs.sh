#!/bin/bash
set -eux

echo "Generating data for testing..."

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

cd "$ROOT_DIR"

mtt train options.yaml -o model-32-bit.pt -r base_precision=32
mtt train options.yaml -o model-64-bit.pt -r base_precision=64
mtt train options-pet.yaml -o model-pet.pt

# upload results to private HF repo if token is set
if [[ "${HUGGINGFACE_TOKEN_METATRAIN+}" != "" ]]; then
    huggingface-cli upload \
        "metatensor/metatrain-test" \
        "model-32-bit.ckpt" \
        "model.ckpt" \
        --commit-message="Overwrite test model with new version" \
        --token="$HUGGINGFACE_TOKEN_METATRAIN"
fi
