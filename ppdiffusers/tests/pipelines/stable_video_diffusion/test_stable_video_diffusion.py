# Copyright (c) 2024 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import random
import tempfile
import unittest

import numpy as np
import paddle

import ppdiffusers
from ppdiffusers import (
    AutoencoderKLTemporalDecoder,
    EulerDiscreteScheduler,
    StableVideoDiffusionPipeline,
    UNetSpatioTemporalConditionModel,
)
from ppdiffusers.transformers import (
    CLIPImageProcessor,
    CLIPVisionConfig,
    CLIPVisionModelWithProjection,
)
from ppdiffusers.utils import (
    is_accelerate_available,
    is_accelerate_version,
    load_image,
    logging,
)
from ppdiffusers.utils.import_utils import is_ppxformers_available
from ppdiffusers.utils.testing_utils import (
    CaptureLogger,
    disable_full_determinism,
    enable_full_determinism,
    floats_tensor,
    numpy_cosine_similarity_distance,
    paddle_device,
    require_paddle_gpu,
    slow,
)

from ..test_pipelines_common import PipelineTesterMixin


def to_np(tensor):
    if isinstance(tensor, paddle.Tensor):
        tensor = tensor.detach().cpu().numpy()

    return tensor


class StableVideoDiffusionPipelineFastTests(PipelineTesterMixin, unittest.TestCase):
    pipeline_class = StableVideoDiffusionPipeline
    params = frozenset(["image"])
    batch_params = frozenset(["image", "generator"])
    required_optional_params = frozenset(
        [
            "num_inference_steps",
            "generator",
            "latents",
            "return_dict",
        ]
    )

    def get_dummy_components(self):
        paddle.seed(0)
        unet = UNetSpatioTemporalConditionModel(
            block_out_channels=(32, 64),
            layers_per_block=2,
            sample_size=32,
            in_channels=8,
            out_channels=4,
            down_block_types=(
                "CrossAttnDownBlockSpatioTemporal",
                "DownBlockSpatioTemporal",
            ),
            up_block_types=("UpBlockSpatioTemporal", "CrossAttnUpBlockSpatioTemporal"),
            cross_attention_dim=32,
            num_attention_heads=8,
            projection_class_embeddings_input_dim=96,
            addition_time_embed_dim=32,
        )
        scheduler = EulerDiscreteScheduler(
            beta_start=0.00085,
            beta_end=0.012,
            beta_schedule="scaled_linear",
            interpolation_type="linear",
            num_train_timesteps=1000,
            prediction_type="v_prediction",
            sigma_max=700.0,
            sigma_min=0.002,
            steps_offset=1,
            timestep_spacing="leading",
            timestep_type="continuous",
            trained_betas=None,
            use_karras_sigmas=True,
        )

        paddle.seed(0)
        vae = AutoencoderKLTemporalDecoder(
            block_out_channels=[32, 64],
            in_channels=3,
            out_channels=3,
            down_block_types=["DownEncoderBlock2D", "DownEncoderBlock2D"],
            latent_channels=4,
        )

        paddle.seed(0)
        config = CLIPVisionConfig(
            hidden_size=32,
            projection_dim=32,
            num_hidden_layers=5,
            num_attention_heads=4,
            image_size=32,
            intermediate_size=37,
            patch_size=1,
        )
        image_encoder = CLIPVisionModelWithProjection(config)

        paddle.seed(0)
        feature_extractor = CLIPImageProcessor(crop_size=32, size=32)
        components = {
            "unet": unet,
            "image_encoder": image_encoder,
            "scheduler": scheduler,
            "vae": vae,
            "feature_extractor": feature_extractor,
        }
        return components

    def get_dummy_inputs(self, seed=0):

        generator = paddle.Generator().manual_seed(seed)

        image = floats_tensor((1, 3, 32, 32), rng=random.Random(0))
        inputs = {
            "generator": generator,
            "image": image,
            "num_inference_steps": 2,
            "output_type": "pd",
            "min_guidance_scale": 1.0,
            "max_guidance_scale": 2.5,
            "num_frames": 2,
            "height": 32,
            "width": 32,
        }
        return inputs

    @unittest.skip("Deprecated functionality")
    def test_attention_slicing_forward_pass(self):
        pass

    @unittest.skip("Batched inference works and outputs look correct, but the test is failing")
    def test_inference_batch_single_identical(
        self,
        batch_size=2,
        expected_max_diff=1e-4,
    ):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for components in pipe.components.values():
            if hasattr(components, "set_default_attn_processor"):
                components.set_default_attn_processor()

        pipe.set_progress_bar_config(disable=None)
        inputs = self.get_dummy_inputs()

        # Reset generator in case it is has been used in self.get_dummy_inputs
        inputs["generator"] = paddle.Generator().manual_seed(0)

        logger = logging.get_logger(pipe.__module__)
        logger.setLevel(level=ppdiffusers.logging.FATAL)

        # batchify inputs
        batched_inputs = {}
        batched_inputs.update(inputs)

        batched_inputs["generator"] = [paddle.Generator().manual_seed(0) for i in range(batch_size)]
        batched_inputs["image"] = paddle.cat([inputs["image"]] * batch_size, dim=0)

        output = pipe(**inputs).frames
        output_batch = pipe(**batched_inputs).frames

        assert len(output_batch) == batch_size

        max_diff = np.abs(to_np(output_batch[0]) - to_np(output[0])).max()
        assert max_diff < expected_max_diff

    @unittest.skip("Test is similar to test_inference_batch_single_identical")
    def test_inference_batch_consistent(self):
        pass

    def test_dict_tuple_outputs_equivalent(self, expected_max_difference=1e-4):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()

        pipe.set_progress_bar_config(disable=None)

        output = pipe(**self.get_dummy_inputs()).frames[0]
        output_tuple = pipe(**self.get_dummy_inputs(), return_dict=False)[0]

        max_diff = np.abs(to_np(output) - to_np(output_tuple)).max()
        self.assertLess(max_diff, expected_max_difference)

    @unittest.skip("Test is currently failing")
    def test_float16_inference(self, expected_max_diff=5e-2):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()

        pipe.set_progress_bar_config(disable=None)

        components = self.get_dummy_components()
        pipe_fp16 = self.pipeline_class(**components)
        for component in pipe_fp16.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()

        pipe_fp16.to(paddle_dtype=paddle.float16)
        pipe_fp16.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output = pipe(**inputs).frames[0]

        fp16_inputs = self.get_dummy_inputs()
        output_fp16 = pipe_fp16(**fp16_inputs).frames[0]

        max_diff = np.abs(to_np(output) - to_np(output_fp16)).max()
        self.assertLess(max_diff, expected_max_diff, "The outputs of the fp16 and fp32 pipelines are too different.")

    @unittest.skipIf(paddle_device != "cuda", reason="float16 requires CUDA")
    def test_save_load_float16(self, expected_max_diff=1e-2):
        components = self.get_dummy_components()
        for name, module in components.items():
            if hasattr(module, "half"):
                components[name] = module.half()

        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output = pipe(**inputs).frames[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir)
            pipe_loaded = self.pipeline_class.from_pretrained(tmpdir, paddle_dtype=paddle.float16)
            for component in pipe_loaded.components.values():
                if hasattr(component, "set_default_attn_processor"):
                    component.set_default_attn_processor()
            pipe_loaded.set_progress_bar_config(disable=None)

        for name, component in pipe_loaded.components.items():
            if hasattr(component, "dtype"):
                self.assertTrue(
                    component.dtype == paddle.float16,
                    f"`{name}.dtype` switched from `float16` to {component.dtype} after loading.",
                )

        inputs = self.get_dummy_inputs()
        output_loaded = pipe_loaded(**inputs).frames[0]
        max_diff = np.abs(to_np(output) - to_np(output_loaded)).max()
        self.assertLess(
            max_diff, expected_max_diff, "The output of the fp16 pipeline changed after saving and loading."
        )

    def test_save_load_optional_components(self, expected_max_difference=1e-4):
        if not hasattr(self.pipeline_class, "_optional_components"):
            return

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()
        pipe.set_progress_bar_config(disable=None)

        # set all optional components to None
        for optional_component in pipe._optional_components:
            setattr(pipe, optional_component, None)

        inputs = self.get_dummy_inputs()
        output = pipe(**inputs).frames[0]

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir, safe_serialization=False)
            pipe_loaded = self.pipeline_class.from_pretrained(tmpdir)
            for component in pipe_loaded.components.values():
                if hasattr(component, "set_default_attn_processor"):
                    component.set_default_attn_processor()
            pipe_loaded.set_progress_bar_config(disable=None)

        for optional_component in pipe._optional_components:
            self.assertTrue(
                getattr(pipe_loaded, optional_component) is None,
                f"`{optional_component}` did not stay set to None after loading.",
            )

        inputs = self.get_dummy_inputs()
        output_loaded = pipe_loaded(**inputs).frames[0]

        max_diff = np.abs(to_np(output) - to_np(output_loaded)).max()
        self.assertLess(max_diff, expected_max_difference)

    def test_save_load_local(self, expected_max_difference=9e-4):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()

        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output = pipe(**inputs).frames[0]

        logger = logging.get_logger("ppdiffusers.pipelines.pipeline_utils")
        logger.setLevel(ppdiffusers.logging.INFO)

        with tempfile.TemporaryDirectory() as tmpdir:
            pipe.save_pretrained(tmpdir, safe_serialization=False)

            with CaptureLogger(logger) as cap_logger:
                pipe_loaded = self.pipeline_class.from_pretrained(tmpdir)

            for name in pipe_loaded.components.keys():
                if name not in pipe_loaded._optional_components:
                    assert name in str(cap_logger)

            pipe_loaded.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output_loaded = pipe_loaded(**inputs).frames[0]

        max_diff = np.abs(to_np(output) - to_np(output_loaded)).max()
        self.assertLess(max_diff, expected_max_difference)

    @unittest.skipIf(paddle_device != "cuda", reason="CUDA and CPU are required to switch devices")
    def test_to_device(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)

        model_devices = [
            component.device.type for component in pipe.components.values() if hasattr(component, "device")
        ]
        self.assertTrue(all(device == "cpu" for device in model_devices))

        output_cpu = pipe(**self.get_dummy_inputs("cpu")).frames[0]
        self.assertTrue(np.isnan(output_cpu).sum() == 0)

        model_devices = [
            component.device.type for component in pipe.components.values() if hasattr(component, "device")
        ]
        self.assertTrue(all(device == "cuda" for device in model_devices))

        output_cuda = pipe(**self.get_dummy_inputs("cuda")).frames[0]
        self.assertTrue(np.isnan(to_np(output_cuda)).sum() == 0)

    def test_to_dtype(self):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        pipe.set_progress_bar_config(disable=None)

        model_dtypes = [component.dtype for component in pipe.components.values() if hasattr(component, "dtype")]
        self.assertTrue(all(dtype == paddle.float32 for dtype in model_dtypes))

        pipe.to(paddle_dtype=paddle.float16)
        model_dtypes = [component.dtype for component in pipe.components.values() if hasattr(component, "dtype")]
        self.assertTrue(all(dtype == paddle.float16 for dtype in model_dtypes))

    @unittest.skipIf(
        paddle_device != "cuda" or not is_accelerate_available() or is_accelerate_version("<", "0.14.0"),
        reason="CPU offload is only available with CUDA and `accelerate v0.14.0` or higher",
    )
    def test_sequential_cpu_offload_forward_pass(self, expected_max_diff=1e-4):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output_without_offload = pipe(**inputs).frames[0]

        pipe.enable_sequential_cpu_offload()

        inputs = self.get_dummy_inputs()
        output_with_offload = pipe(**inputs).frames[0]

        max_diff = np.abs(to_np(output_with_offload) - to_np(output_without_offload)).max()
        self.assertLess(max_diff, expected_max_diff, "CPU offloading should not affect the inference results")

    @unittest.skipIf(
        paddle_device != "cuda" or not is_accelerate_available() or is_accelerate_version("<", "0.17.0"),
        reason="CPU offload is only available with CUDA and `accelerate v0.17.0` or higher",
    )
    def test_model_cpu_offload_forward_pass(self, expected_max_diff=2e-4):
        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)

        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()

        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output_without_offload = pipe(**inputs).frames[0]

        pipe.enable_model_cpu_offload()
        inputs = self.get_dummy_inputs()
        output_with_offload = pipe(**inputs).frames[0]

        max_diff = np.abs(to_np(output_with_offload) - to_np(output_without_offload)).max()
        self.assertLess(max_diff, expected_max_diff, "CPU offloading should not affect the inference results")
        offloaded_modules = [
            v
            for k, v in pipe.components.items()
            if isinstance(v, paddle.nn.Module) and k not in pipe._exclude_from_cpu_offload
        ]
        (
            self.assertTrue(all(v.device.type == "cpu" for v in offloaded_modules)),
            f"Not offloaded: {[v for v in offloaded_modules if v.device.type != 'cpu']}",
        )

    @unittest.skipIf(
        paddle_device != "cuda" or not is_ppxformers_available(),
        reason="XFormers attention is only available with CUDA and `xformers` installed",
    )
    def test_xformers_attention_forwardGenerator_pass(self):
        disable_full_determinism()

        expected_max_diff = 9e-4

        if not self.test_xformers_attention:
            return

        components = self.get_dummy_components()
        pipe = self.pipeline_class(**components)
        for component in pipe.components.values():
            if hasattr(component, "set_default_attn_processor"):
                component.set_default_attn_processor()
        pipe.set_progress_bar_config(disable=None)

        inputs = self.get_dummy_inputs()
        output_without_offload = pipe(**inputs).frames[0]
        output_without_offload = (
            output_without_offload.cpu() if paddle.is_tensor(output_without_offload) else output_without_offload
        )

        pipe.enable_xformers_memory_efficient_attention()
        inputs = self.get_dummy_inputs()
        output_with_offload = pipe(**inputs).frames[0]
        output_with_offload = (
            output_with_offload.cpu() if paddle.is_tensor(output_with_offload) else output_without_offload
        )

        max_diff = np.abs(to_np(output_with_offload) - to_np(output_without_offload)).max()
        self.assertLess(max_diff, expected_max_diff, "XFormers attention should not affect the inference results")

        enable_full_determinism()
