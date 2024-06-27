import torch
import torch.distributed

from opentelemetry import trace
from typing import Optional
from transformers import AutoTokenizer
from transformers.models.gpt2 import GPT2TokenizerFast

from text_generation_server.models import FlashCausalLM
from text_generation_server.models.custom_modeling.flash_deepseek_v2_modeling import (
    FlashDeepseekV2ForCausalLM,
    DeepseekV2Config,
)
from text_generation_server.utils import (
    initialize_torch_distributed,
    weight_files,
    Weights,
)

tracer = trace.get_tracer(__name__)


class FlashDeepseekV2(FlashCausalLM):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        quantize: Optional[str] = None,
        speculator: Optional[str] = None,
        dtype: Optional[torch.dtype] = None,
        trust_remote_code: bool = False,
    ):
        self.process_group, rank, world_size = initialize_torch_distributed()
        if torch.cuda.is_available():
            device = torch.device(f"cuda:{rank}")
            dtype = torch.bfloat16 if dtype is None else dtype
        else:
            raise NotImplementedError("FlashDeepseekV2 is only available on GPU")

        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            padding_side="left",
            truncation_side="left",
            trust_remote_code=trust_remote_code,
            use_fast=True,
            from_slow=False,
        )

        config = DeepseekV2Config.from_pretrained(
            model_id, revision=revision, trust_remote_code=trust_remote_code
        )
        config.quantize = quantize
        config.speculator = speculator

        torch.distributed.barrier(group=self.process_group)

        filenames = weight_files(model_id, revision=revision, extension=".safetensors")
        weights = Weights(filenames, device, dtype, process_group=self.process_group)
        if config.quantize in ["gptq", "awq", "marlin"]:
            weights._set_gptq_params(model_id, revision)

        model = FlashDeepseekV2ForCausalLM(config, weights)

        torch.distributed.barrier(group=self.process_group)
        super(FlashDeepseekV2, self).__init__(
            model_id=model_id,
            model=model,
            tokenizer=tokenizer,
            num_layers=len(model.model.layers),
            num_kv_heads=model.model.num_key_value_heads,
            # As far as the cache is concerned, the head size is always 256 due to padding.
            head_size=256,
            dtype=dtype,
            device=device,
            rank=rank,
            world_size=world_size,
        )
