import os
import time

import jax
import jax.numpy as jnp
from jax.tree_util import tree_map

import ml_collections

# from absl import logging
import wandb

from jaxpi.samplers import BaseSampler
from jaxpi.logging import Logger
from jaxpi.utils import save_checkpoint

import models
from utils import get_dataset

from abc import ABC, abstractmethod
from functools import partial

import jax.numpy as jnp
from jax import random, pmap, local_device_count

from torch.utils.data import Dataset

class OneDimensionalUniformSampler(BaseSampler):
    def __init__(self, dom, batch_size, rng_key=random.PRNGKey(1234)):
        super().__init__(batch_size, rng_key)
        self.dom = dom
        self.dim = 1

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        batch = random.uniform(
            key,
            shape=(self.batch_size, self.dim),
            minval=self.dom[0],
            maxval=self.dom[1],
        )

        return batch


def train_and_evaluate(config: ml_collections.ConfigDict, workdir: str):
    logger = Logger()
    wandb_config = config.wandb
    wandb.init(project=wandb_config.project, name=wandb_config.name)

    # Problem setup
    r_0 = 0.001  # inner radius
    r_1 = 1      # outer radius
    n_r = 12800    # number of spatial points (old: 128 TODO: INCREASE A LOT?)

    # Get  dataset
    u_ref, r_star = get_dataset(r_0, r_1, n_r)

    # Initial condition (TODO: Looks as though this is for t = 0 in their solution, should we have for x = 0)?
    u0 = u_ref[0]
    u1 = u_ref[-1] # need to add to loss as well? 

    # Define domain
    r0 = r_star[0]
    r1 = r_star[-1]

    dom = jnp.array([r0, r1]) # TODO: used to be 2d, check if creates issues? 

    # Initialize model
    model = models.Laplace(config, u0, u1, r_star)
    # Initialize residual sampler
    res_sampler = iter(OneDimensionalUniformSampler(dom, config.training.batch_size_per_device))

    evaluator = models.LaplaceEvaluator(config, model)

    # jit warm up
    print("Waiting for JIT...")
    for step in range(config.training.max_steps):
        start_time = time.time()

        batch = next(res_sampler)

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

    return model