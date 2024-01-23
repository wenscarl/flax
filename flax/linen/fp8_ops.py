# Copyright 2024 The Flax Authors.
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

import warnings
from functools import partial

from jax import custom_jvp, custom_vjp, lax, random
from jax import numpy as jnp

from flax.linen import initializers, module

OVERWRITE_WITH_GRADIENT = '_overwrite_with_gradient'


def get_fp8_max(fp8_dtype, out_dtype):
  assert fp8_dtype in (jnp.float8_e4m3fn, jnp.float8_e5m2)
  return jnp.finfo(fp8_dtype).max.astype(out_dtype)


def quantize(x, q_dtype, scale, compute_dtype):
  # Explicitly cast the max values to the compute dtype to avoid unnecessary
  # casting to FP32 during the subsequent math operations."
  dtype_max = get_fp8_max(q_dtype, compute_dtype)
  scaled_x = x / jnp.broadcast_to(scale.astype(compute_dtype), x.shape)
  clipped_x = jnp.clip(scaled_x, -dtype_max, dtype_max)
  return clipped_x.astype(q_dtype)


def dequantize(x, dq_dtype, scale):
  return x.astype(dq_dtype) * jnp.broadcast_to(scale.astype(dq_dtype), x.shape)


def quantize_dequantize(x, q_dtype, scale, compute_dtype):
  qx = quantize(x, q_dtype, scale, compute_dtype)
  return dequantize(qx, x.dtype, scale)


def compute_scale(amax, scale, fp8_max, margin=0):
  # The algorithm for computing the new scale is sourced from
  #   https://docs.nvidia.com/deeplearning/transformer-engine/user-guide/api/jax.html#transformer_engine.jax.update_fp8_metas
  # wherein the `original_scale` corresponds to the reciprocal of the `scale`
  # passed in this function.
  scale = 1.0 / scale

  sf = (fp8_max / amax) / (2**margin)
  sf = jnp.where(amax > 0.0, sf, scale)
  sf = jnp.where(jnp.isfinite(amax), sf, scale)

  return 1.0 / sf


def compute_scale_and_amax_history(x, q_dtype, scale, amax_history):
  dtype_max = get_fp8_max(q_dtype, jnp.float32)
  amax_update = jnp.max(jnp.abs(x)).astype(scale.dtype)
  new_history = jnp.roll(amax_history, shift=-1, axis=0).at[0].set(amax_update)
  amax_from_history = jnp.max(new_history, axis=0)
  new_scale = compute_scale(amax_from_history, scale, dtype_max)
  return new_scale, new_history


def qdq_and_return(x, q_dtype, scale, amax_history, compute_dtype):
  qx = quantize_dequantize(x, q_dtype, scale, compute_dtype)
  new_scale, new_history = compute_scale_and_amax_history(
    x, q_dtype, scale, amax_history
  )
  return qx, new_scale, new_history


@partial(custom_vjp, nondiff_argnums=(0,))
def in_qdq(compute_dtype, inp, scale, amax_history):
  qin, _, _ = qdq_and_return(
    inp, jnp.float8_e4m3fn, scale, amax_history, compute_dtype
  )
  return qin


def in_qdq_fwd(compute_dtype, inp, scale, amax_history):
  qin, new_scale, new_history = qdq_and_return(
    inp, jnp.float8_e4m3fn, scale, amax_history, compute_dtype
  )
  return qin, (new_scale, new_history)


def in_qdq_bwd(compute_dtype, res, g):
  new_scale, new_history = res
  q_g = g
  return q_g, new_scale, new_history


in_qdq.defvjp(in_qdq_fwd, in_qdq_bwd)


@partial(custom_vjp, nondiff_argnums=(0,))
def out_qdq(compute_dtype, out, scale, amax_history):
  return out


def out_qdq_fwd(compute_dtype, out, scale, amax_history):
  return out, (scale, amax_history)


def out_qdq_bwd(compute_dtype, res, g):
  scale, amax_history = res
  q_g, new_scale, new_history = qdq_and_return(
    g, jnp.float8_e5m2, scale, amax_history, compute_dtype
  )
  return q_g, new_scale, new_history


out_qdq.defvjp(out_qdq_fwd, out_qdq_bwd)


@partial(custom_jvp, nondiff_argnums=(2, 3, 4))
def dot_general_with_precision(
  lhs, rhs, dimension_numbers, precision=None, preferred_element_type=None
):
  if precision != None or preferred_element_type != None:
    warnings.warn(
      'The function dot_general_with_precision will set the '
      'precision/preferred_element_type and disregard any provided '
      'values.'
    )
  return lax.dot_general(
    lhs, rhs, dimension_numbers, precision=lax.Precision.DEFAULT
  )


@dot_general_with_precision.defjvp
def dot_general_with_precision_jvp(
  dimension_numbers, precision, preferred_element_type, primals, tangents
):
  lhs, rhs = primals
  lhs_dot, rhs_dot = tangents

  out = lax.dot_general(
    lhs, rhs, dimension_numbers, precision=lax.Precision.DEFAULT
  )
  grad_out = lax.dot_general(
    lhs_dot, rhs, dimension_numbers, precision=lax.Precision.HIGHEST
  ) + lax.dot_general(
    lhs, rhs_dot, dimension_numbers, precision=lax.Precision.HIGHEST
  )
  return out, grad_out


class Fp8DotGeneralOp(module.Module):
  amax_history_length: int = 1024

  def setup(self) -> None:
    scale_args = (
      initializers.ones_init(),
      random.PRNGKey(0),
      (1,),
      jnp.float32,
    )
    amax_history_args = (
      initializers.zeros_init(),
      random.PRNGKey(0),
      (self.amax_history_length,),
      jnp.float32,
    )

    self.input_amax_history = self.variable(
      OVERWRITE_WITH_GRADIENT, 'input_amax_history', *amax_history_args
    )
    self.kernel_amax_history = self.variable(
      OVERWRITE_WITH_GRADIENT, 'kernel_amax_history', *amax_history_args
    )
    self.output_grad_amax_history = self.variable(
      OVERWRITE_WITH_GRADIENT, 'output_grad_amax_history', *amax_history_args
    )

    self.input_scale = self.variable(
      OVERWRITE_WITH_GRADIENT, 'input_scale', *scale_args
    )
    self.kernel_scale = self.variable(
      OVERWRITE_WITH_GRADIENT, 'kernel_scale', *scale_args
    )
    self.output_grad_scale = self.variable(
      OVERWRITE_WITH_GRADIENT, 'output_grad_scale', *scale_args
    )

  def __call__(self, *args, use_amax_history=True, **kwargs):
    assert len(args) == 3
    x = args[0]
    k = args[1]
    dimension_numbers = args[2]

    # Use the `k.dtype` since it aligns with the `dtype` of its layers,
    # namely, the computation data type.
    comp_dtype = k.dtype
    x = jnp.asarray(x, comp_dtype)
    if use_amax_history:
      x_qdq = in_qdq(
        comp_dtype, x, self.input_scale.value, self.input_amax_history.value
      )
      k_qdq = in_qdq(
        comp_dtype, k, self.kernel_scale.value, self.kernel_amax_history.value
      )
      y_qdq = dot_general_with_precision(x_qdq, k_qdq, dimension_numbers)  # type: ignore
      y = out_qdq(
        comp_dtype,
        y_qdq,
        self.output_grad_scale.value,
        self.output_grad_amax_history.value,
      )
    else:
      x_qdq = quantize_dequantize(x, jnp.float8_e4m3fn, self.input_scale.value, comp_dtype)
      k_qdq = quantize_dequantize(k, jnp.float8_e4m3fn, self.kernel_scale.value, comp_dtype)
      y = dot_general_with_precision(x_qdq, k_qdq, dimension_numbers)  # type: ignore

    return y  # type: ignore
