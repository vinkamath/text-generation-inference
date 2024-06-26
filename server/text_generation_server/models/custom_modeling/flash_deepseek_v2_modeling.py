# coding=utf-8
# Copyright 2022 HuggingFace Inc. team. All rights reserved.
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

import torch
import torch.distributed

from torch import nn
from transformers.activations import ACT2FN
from transformers.configuration_utils import PretrainedConfig
from typing import Optional, List, Tuple, Any
from text_generation_server.utils.import_utils import SYSTEM

if SYSTEM != "ipex":
    from vllm.model_executor.layers.fused_moe import fused_moe

from text_generation_server.layers.attention import (
    paged_attention,
    attention,
    reshape_and_cache,
)
from text_generation_server.layers import (
    FastLinear,
    TensorParallelRowLinear,
    TensorParallelColumnLinear,
    TensorParallelEmbedding,
    SpeculativeHead,
    get_linear,
)
from text_generation_server.layers.rotary import (
    PositionRotaryEmbedding,
)
from text_generation_server.layers.layernorm import (
    FastRMSNorm,
)
from text_generation_server.utils.log import log_once


class DeepseekV2Config(PretrainedConfig):
    def __init__(
        self,
        vocab_size=102400,
        hidden_size=4096,
        intermediate_size=11008,
        moe_intermediate_size=1407,
        num_hidden_layers=30,
        num_attention_heads=32,
        num_key_value_heads=32,
        n_shared_experts=None,
        n_routed_experts=None,
        ep_size=1,
        routed_scaling_factor=1.0,
        kv_lora_rank=512,
        q_lora_rank=1536,
        qk_rope_head_dim=64,
        v_head_dim=128,
        qk_nope_head_dim=128,
        topk_method="gready",
        n_group=None,
        topk_group=None,
        num_experts_per_tok=None,
        moe_layer_freq=1,
        first_k_dense_replace=0,
        norm_topk_prob=False,
        scoring_func="softmax",
        aux_loss_alpha=0.001,
        seq_aux=True,
        hidden_act="silu",
        max_position_embeddings=2048,
        initializer_range=0.02,
        rms_norm_eps=1e-6,
        use_cache=True,
        pad_token_id=None,
        bos_token_id=100000,
        eos_token_id=100001,
        pretraining_tp=1,
        tie_word_embeddings=False,
        rope_theta=10000.0,
        rope_scaling=None,
        attention_bias=False,
        attention_dropout=0.0,
        **kwargs,
    ):
        self.vocab_size = vocab_size
        self.max_position_embeddings = max_position_embeddings
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.moe_intermediate_size = moe_intermediate_size
        self.num_hidden_layers = num_hidden_layers
        self.num_attention_heads = num_attention_heads
        self.n_shared_experts = n_shared_experts
        self.n_routed_experts = n_routed_experts
        self.ep_size = ep_size
        self.routed_scaling_factor = routed_scaling_factor
        self.kv_lora_rank = kv_lora_rank
        self.q_lora_rank = q_lora_rank
        self.qk_rope_head_dim = qk_rope_head_dim
        self.v_head_dim = v_head_dim
        self.qk_nope_head_dim = qk_nope_head_dim
        self.topk_method = topk_method
        self.n_group = n_group
        self.topk_group = topk_group
        self.num_experts_per_tok = num_experts_per_tok
        self.moe_layer_freq = moe_layer_freq
        self.first_k_dense_replace = first_k_dense_replace
        self.norm_topk_prob = norm_topk_prob
        self.scoring_func = scoring_func
        self.aux_loss_alpha = aux_loss_alpha
        self.seq_aux = seq_aux
        # for backward compatibility
        if num_key_value_heads is None:
            num_key_value_heads = num_attention_heads

        self.num_key_value_heads = num_key_value_heads
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.rms_norm_eps = rms_norm_eps
        self.pretraining_tp = pretraining_tp
        self.use_cache = use_cache
        self.rope_theta = rope_theta
        self.rope_scaling = rope_scaling
        self.attention_bias = attention_bias
        self.attention_dropout = attention_dropout

        tie_word_embeddings = kwargs.pop("tie_word_embeddings", False)
        if tie_word_embeddings:
            raise ValueError(
                "tie_word_embeddings is not supported for DeepseekV2 models."
            )

        super().__init__(
            pad_token_id=pad_token_id,
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

        super().__init__(
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )


def promote_scalar(x: torch.Tensor) -> torch.Tensor:
    return x.view(1) if len(x.size()) == 0 else x


def load_attention(config, prefix, weights):
    return TensorParallelColumnLinear.load_qkv(
        config,
        prefix=f"{prefix}.Wqkv",
        weights=weights,
        bias=False,
        num_heads=config.num_attention_heads,
        num_key_value_heads=config.num_key_value_heads,
    )


def _load_experts(config, prefix, weights):
    world_size = weights.process_group.size()
    rank = weights.process_group.rank()

    assert (
        config.hidden_size % world_size == 0
    ), f"The chosen expert intermediate_size size {config.moe_intermediate_size} is not compatible with sharding on {world_size} shards"

    expert_size = config.moe_intermediate_size
    block_size = expert_size // world_size
    start = rank * block_size
    stop = (rank + 1) * block_size

    tensor = torch.empty(
        (config.n_routed_experts * block_size, config.hidden_size),
        dtype=weights.dtype,
        device=weights.device,
    )

    slice_ = weights._get_slice(f"{prefix}")

    for i in range(config.n_routing_experts):
        offset = i * expert_size
        expert_slice = slice_[start + offset : stop + offset]

        tensor[i * block_size : (i + 1) * block_size] = expert_slice.to(
            dtype=weights.dtype
        ).to(device=weights.device)
    return tensor


def _load_experts_quantized(config, prefix, weights, cls):
    world_size = weights.process_group.size()
    rank = weights.process_group.rank()

    assert (
        config.ffn_config.ffn_hidden_size % world_size == 0
    ), f"The chosen size {config.ffn_config.ffn_hidden_size} is not compatible with sharding on {world_size} shards"

    expert_size = config.ffn_config.ffn_hidden_size
    block_size = expert_size // world_size
    start = rank * block_size
    stop = (rank + 1) * block_size

    slice_ = weights._get_slice(f"{prefix}")

    experts = []
    for i in range(config.ffn_config.moe_num_experts):
        if config.quantize in ["gptq", "awq"]:
            raise NotImplementedError(
                "DeepseekV2 does not support gptq/awq quantization yet."
            )
        else:
            offset = i * expert_size
            expert_slice = (
                slice_[start + offset : stop + offset]
                .to(dtype=weights.dtype)
                .to(device=weights.device)
            )

        if cls == TensorParallelRowLinear:
            expert_slice = expert_slice.t().contiguous()
            linear = get_linear(expert_slice, None, config.quantize)
            experts.append(cls(linear, weights.process_group))
        else:
            linear = get_linear(expert_slice, None, config.quantize)
            experts.append(cls(linear))

    return experts


class DeepseekV2Attention(torch.nn.Module):
    def __init__(
        self,
        prefix: str,
        config,
        weights,
    ):
        super().__init__()
        self.num_heads = config.num_attention_heads
        self.hidden_size = config.hidden_size
        self.head_size = self.hidden_size // self.num_heads

        self.rotary_emb = PositionRotaryEmbedding.static(
            config=config,
            dim=self.head_size,
            base=config.rope_theta,
            device=weights.device,
        )

        self.softmax_scale = self.head_size**-0.5

        if self.num_heads % weights.process_group.size() != 0:
            raise ValueError(
                f"`num_heads` must be divisible by `num_shards` (got `num_heads`: {self.num_heads} "
                f"and `num_shards`: {weights.process_group.size()}"
            )
        self.num_heads = self.num_heads // weights.process_group.size()
        self.num_key_value_heads = (
            config.num_key_value_heads // weights.process_group.size()
        )

        self.query = TensorParallelColumnLinear.load(
            config,
            prefix=f"{prefix}.q_proj",
            weights=weights,
            bias=config.attention_bias,
        )

        self.kv_a_proj_with_mqa = FastLinear.load(
            config,
            prefix=f"{prefix}.kv_a_proj_with_mqa",
            weights=weights,
            bias=config.attention_bias,
        )

        self.kv_a_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.kv_a_layernorm", weights=weights, eps=config.rms_norm_eps
        )

        self.kv_b_proj = TensorParallelColumnLinear.load(
            config,
            prefix=f"{prefix}.kv_b_proj",
            weights=weights,
            bias=config.attention_bias,
        )

        self.o_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.o_proj",
            weights=weights,
            bias=False,
        )
        self.num_groups = self.num_heads // self.num_key_value_heads
        self.kv_head_mapping = torch.arange(
            0, self.num_key_value_heads, dtype=torch.int32, device=weights.device
        ).repeat_interleave(self.num_groups)

    def forward(
        self,
        hidden_states,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        block_tables,
        slots,
        input_lengths,
        max_s,
    ):
        qkv = self.query_key_value(hidden_states)
        if self.clip_qkv is not None:
            qkv = qkv.clamp(min=-self.clip_qkv, max=self.clip_qkv)

        query, kv = qkv.split(
            [
                self.head_size * self.num_heads,
                2 * self.head_size * self.num_key_value_heads,
            ],
            dim=1,
        )
        query = query.view(-1, self.num_heads, self.head_size)
        kv = kv.view(-1, 2, self.num_key_value_heads, self.head_size)

        self.rotary_emb(query, torch.select(kv, dim=1, index=0), cos, sin)

        reshape_and_cache(kv[:, 0], kv[:, 1], kv_cache[0], kv_cache[1], slots)

        # output tensor
        attn_output = torch.empty_like(query)

        # Prefill
        if cu_seqlen_prefill is not None:
            # flash attention
            attention(
                query,
                torch.select(kv, dim=1, index=0),
                torch.select(kv, dim=1, index=1),
                attn_output,
                cu_seqlen_prefill,
                max_s,
                self.softmax_scale,
            )
        # Decode
        else:
            paged_attention(
                attn_output,
                query,
                kv_cache[0],
                kv_cache[1],
                self.kv_head_mapping,
                self.softmax_scale,
                block_tables,
                input_lengths,
                max_s,
            )

        return self.o_proj(attn_output.view(-1, self.num_heads * self.head_size))


class DeepseekV2MLP(nn.Module):
    def __init__(self, prefix: str, config, weights):
        super().__init__()
        self.hidden_act = config.hidden_act
        self.act = (
            ACT2FN[self.hidden_act]
            if "gelu" not in self.hidden_act
            else lambda x: torch.nn.functional.gelu(
                x,
                approximate=(
                    "tanh"
                    if self.hidden_act in ["gelu_fast", "gelu_pytorch_tanh"]
                    else "none"
                ),
            )
        )

        self.gate_up_proj = TensorParallelColumnLinear.load_multi(
            config,
            prefixes=[f"{prefix}.gate_proj", f"{prefix}.up_proj"],
            weights=weights,
            dim=0,
            bias=False,
        )

        self.down_proj = TensorParallelRowLinear.load(
            config,
            prefix=f"{prefix}.down_proj",
            weights=weights,
            bias=False,
        )

        self.intermediate_size = (
            config.intermediate_size // weights.process_group.size()
        )

        # TODO: This is a hotfix to be removed & properly refactored.
        self.quantize = config.quantize

    def forward(self, hidden_states, adapter_data):
        if (
            SYSTEM == "rocm"
            and self.hidden_act == "silu"
            and hidden_states.shape[0] == 1
            and not self.quantize
        ):
            out = torch.empty(
                hidden_states.shape[0],
                self.intermediate_size,
                dtype=hidden_states.dtype,
                device="cuda",
            )
            _custom_C.LLMM_Silu(self.gate_up_proj.linear.weight, hidden_states, out, 8)
            return self.down_proj(out, adapter_data)
        else:
            gate_up_states = self.gate_up_proj(hidden_states, adapter_data)
            gate_up_states = gate_up_states.view(-1, 2, self.intermediate_size)
            return self.down_proj(
                self.act(gate_up_states[:, 0]) * gate_up_states[:, 1], adapter_data
            )


@torch.jit.script
def select_experts(
    gate_logits: torch.Tensor, top_k: int, moe_normalize_expert_weights: int
):
    # all_probs: (sequence_length, n_experts) and upcast for softmax
    all_probs = torch.nn.functional.softmax(gate_logits, dim=1, dtype=torch.float)
    # weights, selected_experts: (sequence_length, top-k)
    weights, selected_experts = torch.topk(all_probs, top_k, dim=-1)
    if moe_normalize_expert_weights:
        weights = weights / torch.norm(
            weights, p=moe_normalize_expert_weights, dim=-1, keepdim=True
        )
    weights = weights.view(-1)
    selected_experts = selected_experts.view(-1)

    return selected_experts, weights


@torch.jit.script
def round_up(x: torch.Tensor, value: int):
    return torch.div(x + (value - 1), value, rounding_mode="trunc") * value


class BlockSparseMoE(nn.Module):
    def __init__(self, prefix, config: DeepseekV2Config, weights):
        super().__init__()
        self.hidden_dim = config.hidden_size
        self.ffn_dim = config.intermediate_size // weights.process_group.size()
        self.n_experts = config.n_routed_experts
        self.top_k = config.num_experts_per_tok
        self.routed_scaling_factor = config.routed_scaling_factor

        self.experts = nn.ModuleList(
            [
                DeepseekV2MLP(
                    prefix=f"{prefix}.experts.{idx}", config=config, weights=weights
                )
                for idx in range(self.n_experts)
            ]
        )

        # gating
        self.gate = FastLinear.load(config, f"{prefix}.gate", weights, bias=False)

        if config.n_shared_experts is not None:
            self.shared_experts = DeepseekV2MLP(
                prefix=f"{prefix}.shared_experts", config=config, weights=weights
            )

        self.process_group = weights.process_group

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # router_logits: (num_tokens, n_experts)
        router_logits = self.gate(x)
        out = fused_moe(
            x,
            self.wv1,
            self.w2,
            router_logits,
            self.top_k,
            renormalize=self.moe_normalize_expert_weights,
            inplace=True,
        )

        # Reduce sum
        if self.process_group.size() > 1:
            torch.distributed.all_reduce(out, group=self.process_group)

        return out.view(*x.shape)


class DenseMoE(nn.Module):
    def __init__(self, prefix, config: DeepseekV2Config, weights):
        super().__init__()

        self.moe_normalize_expert_weights = (
            config.ffn_config.moe_normalize_expert_weights
        )
        self.hidden_dim = config.d_model
        self.ffn_dim = config.ffn_config.ffn_hidden_size // weights.process_group.size()
        self.num_experts = config.ffn_config.moe_num_experts
        self.top_k = config.ffn_config.moe_top_k

        act = config.ffn_config.ffn_act_fn["name"]
        if "gelu" in act:
            self.act = lambda x: torch.nn.functional.gelu(
                x,
                approximate=(
                    "tanh" if act in ["gelu_fast", "gelu_pytorch_tanh"] else "none"
                ),
            )
        elif "silu" in act:
            self.act = torch.nn.functional.silu
        else:
            self.act = ACT2FN[act]

        # gating
        self.gate = FastLinear.load(
            config, f"{prefix}.router.layer", weights, bias=False
        )

        self.w1 = _load_experts_quantized(
            config,
            prefix=f"{prefix}.experts.mlp.w1",
            weights=weights,
            cls=TensorParallelColumnLinear,
        )
        self.w2 = _load_experts_quantized(
            config,
            prefix=f"{prefix}.experts.mlp.w2",
            weights=weights,
            cls=TensorParallelRowLinear,
        )
        self.v1 = _load_experts_quantized(
            config,
            prefix=f"{prefix}.experts.mlp.v1",
            weights=weights,
            cls=TensorParallelColumnLinear,
        )

        self.process_group = weights.process_group

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (sequence_length, model_dim)
        gate_logits: (sequence_length, n_experts)
        """
        # optional reshape
        input_shape = x.shape
        x = x.view(-1, input_shape[-1])

        # gate_logits: (sequence_length, n_experts)
        gate_logits = self.gate(x)
        # all_probs: (sequence_length, n_experts) and upcast for softmax
        weights = torch.nn.functional.softmax(gate_logits, dim=1, dtype=torch.float)

        if self.top_k < self.num_experts:
            _, not_selected_experts = torch.topk(
                weights,
                self.num_experts - self.top_k,
                largest=False,
                sorted=False,
                dim=1,
            )
            # Mask not selected experts
            weights.scatter_(1, not_selected_experts, 0)

        # Re-normalize
        if self.moe_normalize_expert_weights:
            weights = weights / torch.norm(
                weights, p=self.moe_normalize_expert_weights, dim=-1, keepdim=True
            )
        weights = weights.to(x.dtype)

        # Final output tensor
        out = x.new_zeros(x.shape[0], self.hidden_dim)
        for i in range(self.num_experts):
            h = self.act(self.w1[i](x)) * self.v1[i](x)
            h = self.w2[i](h, reduce=False)
            # Add expert output to out with masking
            out += h * weights[:, i].view(-1, 1)

        # Reduce sum
        if self.process_group.size() > 1:
            torch.distributed.all_reduce(out, group=self.process_group)

        return out


class DeepseekV2Layer(nn.Module):
    def __init__(self, layer_id, config, weights):
        super().__init__()
        prefix = f"model.layers.{layer_id}"

        self.self_attn = DeepseekV2Attention(
            prefix=f"{prefix}.self_attn",
            config=config,
            weights=weights,
        )

        if (
            config.n_routed_experts is not None
            and layer_id >= config.first_k_dense_replace
            and layer_id % config.moe_layer_freq == 0
        ):
            moe_cls = BlockSparseMoE if config.quantize is None else DenseMoE
            self.mlp = moe_cls(f"{prefix}.mlp", config, weights)
        else:
            self.mlp = DeepseekV2MLP(
                prefix=f"{prefix}.mlp", config=config, weights=weights
            )

        self.input_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.input_layernorm", weights=weights, eps=config.rms_norm_eps
        )
        self.post_attention_layernorm = FastRMSNorm.load(
            prefix=f"{prefix}.post_attention_layernorm",
            weights=weights,
            eps=config.rms_norm_eps,
        )

    def forward(
        self,
        hidden_states,
        residual,
        cos,
        sin,
        cu_seqlen_prefill,
        kv_cache,
        block_tables,
        slots,
        input_lengths,
        max_s,
    ):

        normed_hidden_states, res = self.input_layernorm(hidden_states, residual)

        # Self Attention
        attn_output = self.self_attn(
            normed_hidden_states,
            cos,
            sin,
            cu_seqlen_prefill,
            kv_cache,
            block_tables,
            slots,
            input_lengths,
            max_s,
        )

        # faster post attention rms norm
        normed_attn_res_output, attn_res = self.post_attention_layernorm(
            attn_output, res
        )

        moe_output = self.moe(normed_attn_res_output)

        return moe_output, attn_res


class DeepseekV2Model(torch.nn.Module):
    def __init__(self, config, weights):
        super().__init__()

        self.embed_tokens = TensorParallelEmbedding(
            prefix="model.embed_tokens", weights=weights
        )

        self.layers = nn.ModuleList(
            [
                DeepseekV2Layer(
                    layer_id,
                    config,
                    weights,
                )
                for layer_id in range(config.num_hidden_layers)
            ]
        )
        self.norm = FastRMSNorm.load(
            prefix="model.norm", weights=weights, eps=config.rms_norm_eps
        )

        self.head_size = self.layers[0].self_attn.head_size
        self.num_heads = self.layers[0].self_attn.num_heads
        self.num_key_value_heads = self.layers[0].self_attn.num_key_value_heads

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        block_tables: torch.Tensor,
        slots: torch.Tensor,
        input_lengths: torch.Tensor,
        max_s: int,
    ) -> torch.Tensor:
        hidden_states = self.embed_tokens(input_ids)

        # Get rotary cos and sin for this forward
        # Avoid to index in each layer
        cos, sin = self.layers[0].attn.self_attn.rotary_emb.get_cos_sin(
            position_ids, max_s, hidden_states.dtype
        )

        residual = None
        for i, layer in enumerate(self.layers):
            hidden_states, residual = layer(
                hidden_states,
                residual,
                cos,
                sin,
                cu_seqlen_prefill,
                kv_cache[i],
                block_tables,
                slots,
                input_lengths,
                max_s,
            )

        hidden_states, _ = self.norm(hidden_states, residual)

        return hidden_states


class FlashDeepseekV2ForCausalLM(torch.nn.Module):
    def __init__(self, config, weights):
        super().__init__()

        self.model = DeepseekV2Model(config, weights)
        self.lm_head = SpeculativeHead.load(
            config,
            prefix="lm_head",
            weights=weights,
        )

    def forward(
        self,
        input_ids: torch.Tensor,
        position_ids: torch.Tensor,
        cu_seqlen_prefill: Optional[torch.Tensor],
        kv_cache: List[Tuple[torch.Tensor, torch.Tensor]],
        block_tables: torch.Tensor,
        slots: torch.Tensor,
        input_lengths: torch.Tensor,
        max_s: int,
        prefill_cache_indices: Optional[torch.Tensor],
        lm_head_indices: Optional[torch.Tensor] = None,
        adapter_data: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        hidden_states = self.model(
            input_ids,
            position_ids,
            cu_seqlen_prefill,
            kv_cache,
            block_tables,
            slots,
            input_lengths,
            max_s,
        )
        if lm_head_indices is not None:
            hidden_states = hidden_states[lm_head_indices]
        logits, speculative_logits = self.lm_head(hidden_states)
        return logits, speculative_logits
