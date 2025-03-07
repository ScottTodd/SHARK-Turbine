# Copyright 2024 Advanced Micro Devices, Inc
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import os
import sys

import torch
from torch.fx.experimental.proxy_tensor import make_fx
from shark_turbine.aot import *
from iree import runtime as ireert
import iree.compiler as ireec
from iree.compiler.ir import Context
import numpy as np

from turbine_models.custom_models.sd_inference import utils
from diffusers import (
    UNet2DConditionModel,
)

import safetensors
import argparse

from turbine_models.turbine_tank import turbine_tank

parser = argparse.ArgumentParser()
parser.add_argument(
    "--hf_auth_token", type=str, help="The Hugging Face auth token, required"
)
parser.add_argument(
    "--hf_model_name",
    type=str,
    help="HF model name",
    default="CompVis/stable-diffusion-v1-4",
)
parser.add_argument(
    "--scheduler_id",
    type=str,
    help="Scheduler ID",
    default="PNDM",
)
parser.add_argument(
    "--num_inference_steps", type=int, default=50, help="Number of inference steps"
)
parser.add_argument(
    "--batch_size", type=int, default=1, help="Batch size for inference"
)
parser.add_argument(
    "--height", type=int, default=512, help="Height of Stable Diffusion"
)
parser.add_argument("--width", type=int, default=512, help="Width of Stable Diffusion")
parser.add_argument("--compile_to", type=str, help="torch, linalg, vmfb")
parser.add_argument("--external_weight_path", type=str, default="")
parser.add_argument(
    "--external_weights",
    type=str,
    default=None,
    help="saves ir/vmfb without global weights for size and readability, options [safetensors]",
)
parser.add_argument("--device", type=str, default="cpu", help="cpu, cuda, vulkan, rocm")
# TODO: Bring in detection for target triple
parser.add_argument(
    "--iree_target_triple",
    type=str,
    default="",
    help="Specify vulkan target triple or rocm/cuda target device.",
)
parser.add_argument("--vulkan_max_allocation", type=str, default="4294967296")


class Scheduler(torch.nn.Module):
    def __init__(self, hf_model_name, num_inference_steps, scheduler):
        super().__init__()
        self.scheduler = scheduler
        self.scheduler.set_timesteps(num_inference_steps)
        self.unet = UNet2DConditionModel.from_pretrained(
            hf_model_name,
            subfolder="unet",
        )
        self.guidance_scale = 7.5

    def forward(self, latents, encoder_hidden_states) -> torch.FloatTensor:
        latents = latents * self.scheduler.init_noise_sigma
        for t in self.scheduler.timesteps:
            latent_model_input = torch.cat([latents] * 2)
            t = t.unsqueeze(0)
            latent_model_input = self.scheduler.scale_model_input(
                latent_model_input, timestep=t
            )
            unet_out = self.unet.forward(
                latent_model_input, t, encoder_hidden_states, return_dict=False
            )[0]
            noise_pred_uncond, noise_pred_text = unet_out.chunk(2)
            noise_pred = noise_pred_uncond + self.guidance_scale * (
                noise_pred_text - noise_pred_uncond
            )
            latents = self.scheduler.step(noise_pred, t, latents, return_dict=False)[0]
        return latents


def export_scheduler(
    scheduler,
    hf_model_name,
    batch_size,
    height,
    width,
    hf_auth_token=None,
    compile_to="torch",
    external_weights=None,
    external_weight_path=None,
    device=None,
    target_triple=None,
    max_alloc=None,
    upload_ir=False,
):
    mapper = {}
    utils.save_external_weights(
        mapper, scheduler, external_weights, external_weight_path
    )

    encoder_hidden_states_sizes = (2, 77, 768)
    if hf_model_name == "stabilityai/stable-diffusion-2-1-base":
        encoder_hidden_states_sizes = (2, 77, 1024)

    sample = (batch_size, 4, height // 8, width // 8)

    class CompiledScheduler(CompiledModule):
        if external_weights:
            params = export_parameters(
                scheduler, external=True, external_scope="", name_mapper=mapper.get
            )
        else:
            params = export_parameters(scheduler)

        def main(
            self,
            sample=AbstractTensor(*sample, dtype=torch.float32),
            encoder_hidden_states=AbstractTensor(
                *encoder_hidden_states_sizes, dtype=torch.float32
            ),
        ):
            return jittable(scheduler.forward)(sample, encoder_hidden_states)

    import_to = "INPUT" if compile_to == "linalg" else "IMPORT"
    inst = CompiledScheduler(context=Context(), import_to=import_to)

    module_str = str(CompiledModule.get_mlir_module(inst))
    safe_name = utils.create_safe_name(hf_model_name, "-scheduler")
    if upload_ir:
        with open(f"{safe_name}.mlir", "w+") as f:
            f.write(module_str)
        model_name_upload = hf_model_name.replace("/", "-")
        model_name_upload = model_name_upload + "_scheduler"
        turbine_tank.uploadToBlobStorage(
            str(os.path.abspath(f"{safe_name}.mlir")),
            f"{model_name_upload}/{model_name_upload}.mlir",
        )
    if compile_to != "vmfb":
        return module_str
    else:
        utils.compile_to_vmfb(module_str, device, target_triple, max_alloc, safe_name)


if __name__ == "__main__":
    args = parser.parse_args()
    schedulers = utils.get_schedulers(args.hf_model_name)
    scheduler = schedulers[args.scheduler_id]
    scheduler_module = Scheduler(
        args.hf_model_name, args.num_inference_steps, scheduler
    )
    mod_str = export_scheduler(
        scheduler_module,
        args.hf_model_name,
        args.batch_size,
        args.height,
        args.width,
        args.hf_auth_token,
        args.compile_to,
        args.external_weights,
        args.external_weight_path,
        args.device,
        args.iree_target_triple,
        args.vulkan_max_allocation,
    )
    safe_name = utils.create_safe_name(args.hf_model_name, "-scheduler")
    with open(f"{safe_name}.mlir", "w+") as f:
        f.write(mod_str)
    print("Saved to", safe_name + ".mlir")
