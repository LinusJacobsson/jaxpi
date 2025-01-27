import os
import time

import jax
import jax.numpy as jnp
from jax.tree_util import tree_map
from jax import vmap

import ml_collections

# from absl import logging
import wandb

from jaxpi.samplers import BaseSampler, init_sampler
from jaxpi.logging import Logger
from jaxpi.utils import save_checkpoint

import models
from utils import get_dataset

from abc import ABC, abstractmethod
from functools import partial

import jax.numpy as jnp
from jax import random, pmap, local_device_count
from eval import evaluate
from torch.utils.data import Dataset
import matplotlib.pyplot as plt



def train_and_evaluate(config: ml_collections.ConfigDict, workdir: str):
    logger = Logger()
    wandb_config = config.wandb
    wandb.init(project=wandb_config.project, name=wandb_config.name)

    # Problem setup
    r_0 = config.setting.r_0      # inner radius
    r_1 = config.setting.r_1      # outer radius
    n_r = config.setting.n_r    # number of spatial points (old: 128 TODO: INCREASE A LOT?)

    # Get  dataset
    u_ref, r_star = get_dataset(r_0, r_1, n_r)

    # Define domain
    r0 = r_star[0]
    r1 = r_star[-1]

    dom = jnp.array([r0, r1])

    # Initialize model
    model = models.Laplace(config, r_star)

    # Initialize residual sampler. starting with uniform sampling 
    #sampler = OneDimensionalUniformSampler(dom, config.training.batch_size_per_device)
    sampler = init_sampler(model, config)
    res_sampler = iter(sampler)

    evaluator = models.LaplaceEvaluator(config, model)
    # jit warm up
    print("Waiting for JIT...")
    for step in range(config.training.max_steps):
        
        start_time = time.time()
    
        # Update RAD points
        if config.sampler.sampler_name != "random":
            if step % config.sampler.resample_every_steps == 0 and step != 0:
                
                if config.sampler.sampler_name == "rad-cosine": #and step!= config.sampler.resample_every_steps: 
                    #jax.debug.print("Resampling with rad-cosine and passign prev sampler")
                    sampler = init_sampler(model, config, prev = sampler)    
                else:
                    sampler = init_sampler(model, config)

                res_sampler = iter(sampler)
                
                if config.sampler.plot_rad == True:
                    sampler.plot(workdir, step, config.wandb.name)
                

        batch = next(res_sampler)
        
        if config.sampler.plot_batch == True:
            # plot histogram of new batch
            fig = plt.figure(figsize=(8, 8))
            plt.xlabel('Radius [m]')
            plt.ylabel('Count')
            plt.title('Batch histogram')
            plt.hist(batch.flatten(), bins=50, label='Sampled data', color='blue')
            plt.grid()
            plt.legend()
            plt.tight_layout()
            # Save the figure
            save_dir = os.path.join(workdir, "figures", config.wandb.name)
            if not os.path.isdir(save_dir):
                os.makedirs(save_dir)
            fig_path = os.path.join(save_dir, f"batch_hist_{step}.png")
            fig.savefig(fig_path, bbox_inches="tight", dpi=800)
            plt.close(fig)

        model.state = model.step(model.state, batch)

        # Update weights
        if config.weighting.scheme in ["grad_norm", "ntk"]:
            if step % config.weighting.update_every_steps == 0:
                model.state = model.update_weights(model.state, batch)

        # Log training metrics, only use host 0 to record results
        if jax.process_index() == 0:
            if step % config.logging.log_every_steps == 0:
                # Get the first replica of the state and batch
                state = jax.device_get(tree_map(lambda x: x[0], model.state))
                batch = jax.device_get(tree_map(lambda x: x[0], batch))
                log_dict = evaluator(state, batch, u_ref)
                wandb.log(log_dict, step)
                end_time = time.time()

                logger.log_iter(step, start_time, end_time, log_dict)

        # Saving
        if config.saving.save_every_steps is not None:
            if (step + 1) % config.saving.save_every_steps == 0 or (
                step + 1
            ) == config.training.max_steps:
                path = os.path.join(workdir, "ckpt", config.wandb.name)
                save_checkpoint(model.state, path, keep=config.saving.num_keep_ckpts)
                if config.saving.plot == True:
                    evaluate(config, workdir, step +1)


    return model