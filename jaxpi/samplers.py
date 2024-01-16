from abc import ABC, abstractmethod
from functools import partial

import jax
import jax.numpy as jnp
from jax import random, pmap, local_device_count
from jax.tree_util import tree_map

from torch.utils.data import Dataset


# Function for initializing sampler from config file
# argument: sampler name from config file 
def init_sampler(model, sampler: str, **kwargs):
    if sampler == "rad":
        return OneDimensionalRadSampler(model, **kwargs)
    elif sampler == "rad2":
        return OneDimensionalRadSamplerTwo(**kwargs)
    else:     
        raise NotImplementedError(f"Sampler {sampler} not implemented!")


class BaseSampler(Dataset):
    def __init__(self, batch_size, rng_key=random.PRNGKey(1234)):
        self.batch_size = batch_size
        self.key = rng_key
        self.num_devices = local_device_count()

    def __getitem__(self, index):
        "Generate one batch of data"
        self.key, subkey = random.split(self.key)
        keys = random.split(subkey, self.num_devices)
        batch = self.data_generation(keys)
        return batch

    def data_generation(self, key):
        raise NotImplementedError("Subclasses should implement this!")


class UniformSampler(BaseSampler):
    def __init__(self, dom, batch_size, rng_key=random.PRNGKey(1234)):
        super().__init__(batch_size, rng_key)
        self.dom = dom
        self.dim = dom.shape[0]

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        batch = random.uniform(
            key,
            shape=(self.batch_size, self.dim),
            minval=self.dom[:, 0],
            maxval=self.dom[:, 1],
        )

        return batch

# 
class OneDimensionalRadSampler(BaseSampler):
    def __init__(self, model, x, probs, batch_size, rng_key=random.PRNGKey(1234)):
        super().__init__(batch_size, rng_key)
        self.dim = 1
        self.x = x
        self.probs = probs
        self.model = model 

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        state = jax.device_get(tree_map(lambda x: x[0], self.model.state))
        params = state.params
        r_eval = jnp.linspace(0.001, 0.5, 10_000)
        res_pred = jnp.abs(self.model.r_pred_fn(params, r_eval)) # Verify shape on r_eval
        norm_r_eval = res_pred / jnp.sum(res_pred)
        
        batch = random.choice(key, r_eval, shape=(self.batch_size,), p=norm_r_eval) 
        batch = batch.reshape(-1,1)
        return batch

class OneDimensionalRadSamplerTwo(BaseSampler):
    # Imporved RAD

    def __init__(self, x, y_hat, batch_size, c, k, rng_key=random.PRNGKey(1234)):
        super().__init__(batch_size, rng_key)
        self.dim = 1
        self.x = x
        self.y_hat = y_hat
        self.c = c 
        self.k = k

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        prob = jnp.power(self.y_hat, self.k) / jnp.power(self.y_hat, self.k).mean() + self.c
        norm_prob = prob / prob.sum()
        batch = random.choice(key, self.x, shape=(self.batch_size,), p=norm_prob) 
        batch = batch.reshape(-1,1)
        return batch

class SpaceSampler(BaseSampler):
    def __init__(self, coords, batch_size, rng_key=random.PRNGKey(1234)):
        super().__init__(batch_size, rng_key)
        self.coords = coords

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        idx = random.choice(key, self.coords.shape[0], shape=(self.batch_size,))
        batch = self.coords[idx, :]

        return batch


class TimeSpaceSampler(BaseSampler):
    def __init__(
        self, temporal_dom, spatial_coords, batch_size, rng_key=random.PRNGKey(1234)
    ):
        super().__init__(batch_size, rng_key)

        self.temporal_dom = temporal_dom
        self.spatial_coords = spatial_coords

    @partial(pmap, static_broadcasted_argnums=(0,))
    def data_generation(self, key):
        "Generates data containing batch_size samples"
        key1, key2 = random.split(key)

        temporal_batch = random.uniform(
            key1,
            shape=(self.batch_size, 1),
            minval=self.temporal_dom[0],
            maxval=self.temporal_dom[1],
        )

        spatial_idx = random.choice(
            key2, self.spatial_coords.shape[0], shape=(self.batch_size,)
        )
        spatial_batch = self.spatial_coords[spatial_idx, :]
        batch = jnp.concatenate([temporal_batch, spatial_batch], axis=1)

        return batch
