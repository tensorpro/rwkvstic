from rwkvstic.agnostic.backends.modules.base import RwkvModule
from rwkvstic.agnostic.backends.modules.layernorm import LayerNorm
from rwkvstic.agnostic.backends.modules.matmul import MM8, MM8_3
import torch
class Block(RwkvModule):
            def __init__(self, dims, w, i, device, maxvram, dtype, runtimedtype):
                super(Block, self).__init__()

                self.dtype = dtype
                self.runtimedtype = runtimedtype
                self.t = []

                
                self.ln1 = LayerNorm(
                    w[f"blocks.{i}.ln1.weight"], w[f"blocks.{i}.ln1.bias"], device, dtype=runtimedtype)
                self.ln2 = LayerNorm(
                    w[f"blocks.{i}.ln2.weight"], w[f"blocks.{i}.ln2.bias"], device, dtype=runtimedtype)
               
                
                self.att = MM8_3(
                    w[f"blocks.{i}.att.key.weight"],
                    w[f"blocks.{i}.att.value.weight"],
                    w[f"blocks.{i}.att.receptance.weight"],
                    device, maxvram, self.runtimedtype,self.dtype)

                self.ffnkey = MM8(
                    w[f"blocks.{i}.ffn.key.weight"], device, maxvram,
                    self.runtimedtype,self.dtype)
                self.ffnvalue = MM8(
                    w[f"blocks.{i}.ffn.value.weight"], device, maxvram,
                    self.runtimedtype,self.dtype)
                self.attout = MM8(
                    w[f"blocks.{i}.att.output.weight"], device, maxvram,
                    self.runtimedtype,self.dtype)
                self.ffnreceptance = MM8(
                    w[f"blocks.{i}.ffn.receptance.weight"], device, maxvram,
                    self.runtimedtype,self.dtype)
                
                self.attmix= torch.stack((w[f"blocks.{i}.att.time_mix_k"].squeeze(),
                  w[f"blocks.{i}.att.time_mix_v"].squeeze(),
                     w[f"blocks.{i}.att.time_mix_r"].squeeze())).unsqueeze(1).to(self.runtimedtype).clone().to(device)
                
                self.time_first = w[f"blocks.{i}.att.time_first"].squeeze().to(self.runtimedtype).clone().to(device)

                self.time_decay = w[f"blocks.{i}.att.time_decay"].squeeze().double().exp().neg().clone().to(device).to(self.runtimedtype)

                # self.t = [powerTri(self.time_decay, i) for i in range(1, 21)]


                self.ffnmix= torch.stack((w[f"blocks.{i}.ffn.time_mix_k"].squeeze(),
                    w[f"blocks.{i}.ffn.time_mix_r"].squeeze())).unsqueeze(1).to(self.runtimedtype).clone().to(device)
                torch.cuda.empty_cache()
            
            


            @ torch.jit.script_method
            def cuda_wkv(self, T: int, C: int, w, u, k, v, aa, bb, pp):
                assert 1 * C % min(C, 32) == 0
                assert k.dtype == self.runtimedtype
                w = w.contiguous()
                u = u.contiguous()
                k = k.contiguous()
                v = v.contiguous()
                y = torch.empty((T, C), device="cuda", memory_format=torch.contiguous_format, dtype=self.runtimedtype)
                torch.ops.rwkv.wkv_forward(1, T, C, w, u, k, v, y, aa, bb, pp)
                return y.to(self.runtimedtype), aa, bb, pp
            
            @ torch.jit.script_method
            def forward(self, x, state):

                xy = self.ln1(x)

                tc = xy.roll(1, 0)
                rmc = tc[0].copy()
                tc[0] = state[0]
                state[0] = rmc

                mix = torch.lerp(tc.unsqueeze(0), xy.unsqueeze(0), self.attmix)

                k,v,r = self.att(mix)


                r = r.sigmoid()

                # WKV kernel original

                wkv, state[2],state[3],state[4] = self.cuda_wkv(k.shape[0], k.shape[1], self.time_decay, self.time_first, k, v, state[2], state[3], state[4])
               
                wkv *= r

                rz = x + self.attout(wkv)

                ddd = self.ln2(rz)

                rc = ddd.roll(1, 0)
                dc = rc[0].copy()
                rc[0] = state[1]
                state[1] = dc

                fmix = torch.lerp(rc, ddd, self.ffnmix)

                

                rf = self.ffnreceptance(fmix[1]).sigmoid()
                
                kf = self.ffnkey(fmix[0]).relu()
                rvm = self.ffnvalue(torch.square(kf))

                out = rvm * rf + rz

                return out, state

                # stuff
