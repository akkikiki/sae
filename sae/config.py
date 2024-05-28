import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional, cast

import torch
import wandb

from . import __version__


@dataclass
class SaeConfig:
    """
    Configuration for training a sparse autoencoder on a language model.
    """

    # SAE Parameters
    d_in: int
    d_sae: Optional[int] = None
    b_dec_init_method: str = "geometric_median"
    expansion_factor: int = 16
    normalize_sae_decoder: bool = True
    noise_scale: float = 0.0
    from_pretrained_path: Optional[str] = None
    apply_b_dec_to_input: bool = True
    decoder_orthogonal_init: bool = False
    init_encoder_as_decoder_transpose: bool = False

    # Misc
    seed: int = 42
    dtype: torch.dtype = torch.bfloat16

    autocast: bool = True  # autocast to autocast_dtype during training

    ## Batch size
    batch_size: int = 1

    # From https://transformer-circuits.pub/2024/scaling-monosemanticity/index.html
    sparsity_weight: float = 0.85
    lp_norm: float = 1
    l1_warm_up_steps: int = 0

    ## Learning Rate Schedule
    lr: float = 3e-4
    lr_warm_up_steps: int = 40

    # Resampling protocol args
    feature_sampling_window: int = 2000
    dead_feature_window: int = 1000  # unless this window is larger feature sampling,

    dead_feature_threshold: float = 1e-8

    # Evals
    n_eval_batches: int = 10
    eval_batch_size_prompts: int | None = None  # useful if evals cause OOM

    # WANDB
    log_to_wandb: bool = True
    wandb_id: Optional[str] = None
    run_name: Optional[str] = None
    wandb_entity: Optional[str] = None
    wandb_log_frequency: int = 10
    eval_every_n_wandb_logs: int = 100  # logs every 1000 steps.

    # Misc
    resume: bool = False
    n_checkpoints: int = 0
    checkpoint_path: str = "checkpoints"
    verbose: bool = True
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    model_from_pretrained_kwargs: dict[str, Any] = field(default_factory=dict)
    sae_lens_version: str = field(default_factory=lambda: __version__)
    sae_lens_training_version: str = field(default_factory=lambda: __version__)

    def __post_init__(self):
        if self.resume:
            raise ValueError(
                "Resuming is no longer supported. You can finetune a trained SAE using cfg.from_pretrained path."
                + "If you want to load an SAE with resume=True in the config, please manually set resume=False in that config."
            )

        if not isinstance(self.expansion_factor, list):
            self.d_sae = self.d_in * self.expansion_factor

        if self.b_dec_init_method not in ["geometric_median", "mean", "zeros"]:
            raise ValueError(
                f"b_dec_init_method must be geometric_median, mean, or zeros. Got {self.b_dec_init_method}"
            )

        unique_id = self.wandb_id
        if unique_id is None:
            unique_id = cast(
                Any, wandb
            ).util.generate_id()  # not sure why this type is erroring
        self.checkpoint_path = f"{self.checkpoint_path}/{unique_id}"

    def get_checkpoints_by_step(self) -> tuple[dict[int, str], bool]:
        """
        Returns (dict, is_done)
        where dict is [steps] = path
        for each checkpoint, and
        is_done is True if there is a "final_{steps}" checkpoint
        """
        is_done = False
        checkpoints = [
            f
            for f in os.listdir(self.checkpoint_path)
            if os.path.isdir(os.path.join(self.checkpoint_path, f))
        ]
        mapped_to_steps = {}
        for c in checkpoints:
            try:
                steps = int(c)
            except ValueError:
                if c.startswith("final"):
                    steps = int(c.split("_")[1])
                    is_done = True
                else:
                    continue  # ignore this directory
            full_path = os.path.join(self.checkpoint_path, c)
            mapped_to_steps[steps] = full_path
        return mapped_to_steps, is_done

    def get_resume_checkpoint_path(self) -> str:
        """
        Gets the checkpoint path with the most steps
        raises StopIteration if the model is done (there is a final_{steps} directoryh
        raises FileNotFoundError if there are no checkpoints found
        """
        mapped_to_steps, is_done = self.get_checkpoints_by_step()
        if is_done:
            raise StopIteration("Finished training model")
        if len(mapped_to_steps) == 0:
            raise FileNotFoundError("no checkpoints available to resume from")
        else:
            max_step = max(list(mapped_to_steps.keys()))
            checkpoint_dir = mapped_to_steps[max_step]
            print(f"resuming from step {max_step} at path {checkpoint_dir}")
            return mapped_to_steps[max_step]


def load_pretrained_sae_lens_sae_components(
    cfg_path: str, weight_path: str, device: str | torch.device | None = None
) -> tuple[SaeConfig, dict[str, torch.Tensor]]:
    with open(cfg_path, "r") as f:
        config = json.load(f)
    var_names = SaeConfig.__init__.__code__.co_varnames
    # filter config for varnames
    config = {k: v for k, v in config.items() if k in var_names}
    config["verbose"] = False
    config["device"] = device

    # TODO: if we change our SAE implementation such that old versions need conversion to be
    # loaded, we can inspect the original "sae_lens_version" and apply a conversion function here.
    config["sae_lens_version"] = __version__

    config = SaeConfig(**config)

    tensors = {}
    with safe_open(weight_path, framework="pt", device=device) as f:  # type: ignore
        for k in f.keys():
            tensors[k] = f.get_tensor(k)

    # old saves may not have scaling factors.
    if "scaling_factor" not in tensors:
        assert isinstance(config.d_sae, int)
        tensors["scaling_factor"] = torch.ones(
            config.d_sae, dtype=config.dtype, device=next(iter(tensors.values())).device
        )

    return config, tensors
