//===----------------------------------------------------------------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#ifndef __CLC_OPENCL_SHARED_VSTORE_H__
#define __CLC_OPENCL_SHARED_VSTORE_H__

#include <clc/opencl/opencl-base.h>

#define _CLC_VSTORE_DECL(SUFFIX, PRIM_TYPE, VEC_TYPE, WIDTH, ADDR_SPACE, RND)  \
  _CLC_OVERLOAD _CLC_DECL void vstore##SUFFIX##WIDTH##RND(                     \
      VEC_TYPE vec, size_t offset, ADDR_SPACE PRIM_TYPE *out);

#define _CLC_VECTOR_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE, ADDR_SPACE, RND)  \
  _CLC_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE##2, 2, ADDR_SPACE, RND)         \
  _CLC_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE##3, 3, ADDR_SPACE, RND)         \
  _CLC_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE##4, 4, ADDR_SPACE, RND)         \
  _CLC_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE##8, 8, ADDR_SPACE, RND)         \
  _CLC_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE##16, 16, ADDR_SPACE, RND)

#if _CLC_GENERIC_AS_SUPPORTED
#define _CLC_VSTORE_GENERIC_DECL _CLC_VSTORE_DECL
#define _CLC_VECTOR_VSTORE_GENERIC_DECL _CLC_VECTOR_VSTORE_DECL
#else
// The generic address space isn't available, so make the macros do nothing
#define _CLC_VSTORE_GENERIC_DECL(X, Y, Z, W, V, U)
#define _CLC_VECTOR_VSTORE_GENERIC_DECL(X, Y, Z, W, V)
#endif

#define _CLC_VECTOR_VSTORE_PRIM3(SUFFIX, MEM_TYPE, PRIM_TYPE, RND)             \
  _CLC_VECTOR_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE, __private, RND)         \
  _CLC_VECTOR_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE, __local, RND)           \
  _CLC_VECTOR_VSTORE_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE, __global, RND)          \
  _CLC_VECTOR_VSTORE_GENERIC_DECL(SUFFIX, MEM_TYPE, PRIM_TYPE, __generic, RND)

#define _CLC_VECTOR_VSTORE_PRIM1(PRIM_TYPE)                                    \
  _CLC_VECTOR_VSTORE_PRIM3(, PRIM_TYPE, PRIM_TYPE, )

#define _CLC_VECTOR_VSTORE_HALF_PRIM1(PRIM_TYPE, RND)                          \
  _CLC_VSTORE_DECL(_half, half, PRIM_TYPE, , __private, RND)                   \
  _CLC_VSTORE_DECL(_half, half, PRIM_TYPE, , __local, RND)                     \
  _CLC_VSTORE_DECL(_half, half, PRIM_TYPE, , __global, RND)                    \
  _CLC_VSTORE_GENERIC_DECL(_half, half, PRIM_TYPE, , __generic, RND)           \
  _CLC_VECTOR_VSTORE_PRIM3(_half, half, PRIM_TYPE, RND)                        \
  _CLC_VSTORE_DECL(a_half, half, PRIM_TYPE, , __private, RND)                  \
  _CLC_VSTORE_DECL(a_half, half, PRIM_TYPE, , __local, RND)                    \
  _CLC_VSTORE_DECL(a_half, half, PRIM_TYPE, , __global, RND)                   \
  _CLC_VSTORE_GENERIC_DECL(a_half, half, PRIM_TYPE, , __generic, RND)          \
  _CLC_VECTOR_VSTORE_PRIM3(a_half, half, PRIM_TYPE, RND)

_CLC_VECTOR_VSTORE_PRIM1(char)
_CLC_VECTOR_VSTORE_PRIM1(uchar)
_CLC_VECTOR_VSTORE_PRIM1(short)
_CLC_VECTOR_VSTORE_PRIM1(ushort)
_CLC_VECTOR_VSTORE_PRIM1(int)
_CLC_VECTOR_VSTORE_PRIM1(uint)
_CLC_VECTOR_VSTORE_PRIM1(long)
_CLC_VECTOR_VSTORE_PRIM1(ulong)
_CLC_VECTOR_VSTORE_PRIM1(float)

_CLC_VECTOR_VSTORE_HALF_PRIM1(float, )
_CLC_VECTOR_VSTORE_HALF_PRIM1(float, _rtz)
_CLC_VECTOR_VSTORE_HALF_PRIM1(float, _rtn)
_CLC_VECTOR_VSTORE_HALF_PRIM1(float, _rtp)
_CLC_VECTOR_VSTORE_HALF_PRIM1(float, _rte)

#ifdef cl_khr_fp64
_CLC_VECTOR_VSTORE_PRIM1(double)
_CLC_VECTOR_VSTORE_HALF_PRIM1(double, )
_CLC_VECTOR_VSTORE_HALF_PRIM1(double, _rtz)
_CLC_VECTOR_VSTORE_HALF_PRIM1(double, _rtn)
_CLC_VECTOR_VSTORE_HALF_PRIM1(double, _rtp)
_CLC_VECTOR_VSTORE_HALF_PRIM1(double, _rte)
#endif

#ifdef cl_khr_fp16
_CLC_VECTOR_VSTORE_PRIM1(half)
#endif

#undef _CLC_VSTORE_DECL
#undef _CLC_VSTORE_GENERIC_DECL
#undef _CLC_VECTOR_VSTORE_DECL
#undef _CLC_VECTOR_VSTORE_PRIM3
#undef _CLC_VECTOR_VSTORE_PRIM1
#undef _CLC_VECTOR_VSTORE_GENERIC_DECL

#endif // __CLC_OPENCL_SHARED_VSTORE_H__
