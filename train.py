import copy
import functools
import torch
import numpy as np
from torch.optim import AdamW

from utils import dist_util
from utils.fp16_util import (
    zero_grad
)
from utils.nn import update_ema
from utils.step_sample import LossAwareSampler, UniformSampler

class TrainLoop:
    def __init__(
        self,
        *,
        model,
        diffusion,
        data,
        batch_size,
        microbatch,
        lr,
        ema_rate,
        schedule_sampler=None,
        weight_decay=0.0,
        epochs=0,
        eval_data=None,
        eval_interval=-1,
    ):
        self.model = model
        self.diffusion = diffusion
        self.data = data
        self.eval_data = eval_data
        self.batch_size = batch_size
        self.microbatch = microbatch if microbatch > 0 else batch_size
        self.lr = lr
        self.ema_rate = (
            [ema_rate]
            if isinstance(ema_rate, float)
            else [float(x) for x in ema_rate.split(",")]
        )
        self.eval_interval = eval_interval
        self.schedule_sampler = schedule_sampler or UniformSampler(diffusion)
        self.weight_decay = weight_decay
        self.learning_steps = epochs

        self.step = 0

        self.model_params = list(self.model.parameters())
        self.master_params = self.model_params

        self.opt = AdamW(self.master_params, lr=self.lr, weight_decay=self.weight_decay)
        self.ema_params = [copy.deepcopy(self.master_params) for _ in range(len(self.ema_rate))]
        
    def run_loop(self):
        print("\n\n======== Training starts now ========\n\n")
        
        while (
            not self.learning_steps or self.step < self.learning_steps
        ):
            batch, cond = next(self.data)
            self.run_step(batch, cond)
            if self.eval_data is not None and self.step % self.eval_interval == 0:
                batch_eval, cond_eval = next(self.eval_data)
                self.forward_only(batch_eval, cond_eval)
                print('eval on validation set')
            self.step += 1

    def run_step(self, batch, cond):
        self.forward_backward(batch, cond)
        self.optimize_normal()

    def forward_only(self, batch, cond):
        with torch.no_grad():
            zero_grad(self.model_params)
            for i in range(0, batch.shape[0], self.microbatch):
                micro = batch[i: i + self.microbatch].to(dist_util.dev())
                micro_cond = {
                    k: v[i: i + self.microbatch].to(dist_util.dev())
                    for k, v in cond.items()
                }
                last_batch = (i + self.microbatch) >= batch.shape[0]
                t, weights = self.schedule_sampler.sample(micro.shape[0], dist_util.dev())
                # print(micro_cond.keys())
                compute_losses = functools.partial(
                    self.diffusion.training_losses,
                    self.model,
                    micro,
                    t,
                    model_kwargs=micro_cond,
                )

                losses = compute_losses()


    def forward_backward(self, batch, cond):
        all_loss = []
        zero_grad(self.model_params)
        for i in range(0, batch.shape[0], self.microbatch):
            micro = batch[i : i + self.microbatch].to(dist_util.dev())
            micro_cond = {
                k: v[i : i + self.microbatch].to(dist_util.dev())
                for k, v in cond.items()
            }
            last_batch = (i + self.microbatch) >= batch.shape[0]
            t, weights = self.schedule_sampler.sample(micro.shape[0], dist_util.dev())
            # print(micro_cond.keys())
            compute_losses = functools.partial(
                self.diffusion.training_losses,
                self.model,
                micro,
                t,
                model_kwargs=micro_cond,
            )

            losses = compute_losses()
            
            if isinstance(self.schedule_sampler, LossAwareSampler):
                self.schedule_sampler.update_with_local_losses(
                    t, losses["loss"].detach()
                )

            loss = (losses["loss"] * weights).mean()
            loss.backward()
            all_loss.append(loss.detach().cpu())
        print(f'Epoch {self.step} Loss: {np.mean(all_loss)}')

    def optimize_normal(self):
        self._anneal_lr()
        self.opt.step()
        for rate, params in zip(self.ema_rate, self.ema_params):
            update_ema(params, self.master_params, rate=rate)

    def _anneal_lr(self):
        if not self.learning_steps:
            return
        frac_done = self.step / self.learning_steps
        lr = self.lr * (1 - frac_done)
        for param_group in self.opt.param_groups:
            param_group["lr"] = lr