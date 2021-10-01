# flake8: noqa: E501
"""
Example: Large-scale ML Ingest
=================================================

In this example, you will learn how to build, deploy and scale up a machine
learning shuffle ingestion pipeline using
`Ray Dataset <https://docs.ray.io/en/latest/data/dataset.html>`_ and
`Dataset Pipelines <https://docs.ray.io/en/latest/data/dataset-pipeline.html>`_.

In particular, we will show you:

* How to build a shuffle ingestion pipeline that loads, shuffles and feeds data
  into distributed trainers in a few lines of code;
* How to scale the pipeline from ingesting 100MiB data to
  500GiB data.

.. image:: ../../data/dataset-repeat-2.svg
    :align: center

"""

###############################################################################
# Python Setup
# ------------
#
# First, we'll import all of the libraries we'll be using. This step also helps us
# verify that the environment is configured correctly. If any of the imports
# are missing, an exception will be raised.

import argparse
import tempfile
import time
from typing import List

import pandas
import pyarrow

import ray
from ray.data.dataset_pipeline import DatasetPipeline
from ray.data.datasource.datasource import RandomIntRowDatasource, RangeDatasource

#######################################################################
# Build shuffle ingestion pipeline
# ----------------------------------
#
# A typical machine learning ingestion pipeline consists of the following 4
# steps:
#
# 1. Load the training data from external storage;
# 2. Iterate over the data for multiple epochs;
# 3. In each epoch, applying global shuffle to decorrelate the data;
# 4. In each epoch, split the shuffled data into shards, and feed shards to
#    distributed trainers;
#
# Let’s see how we implement such pipeline using Ray Dataset:


def create_shuffle_pipeline(training_data_dir: str, num_epochs: int,
                            num_shards: int) -> List[DatasetPipeline]:

    return ray.data.read_parquet(training_data_dir) \
        .repeat(num_epochs) \
        .random_shuffle_each_window() \
        .split(num_shards, equal=True)


############################################################################
# We’ve now defined a ``create_shuffle_pipeline`` function that creates an
# ingestion pipeline.
# It reads ``training_data_dir``, iterates for ``num_epochs`` times,
# where in each epoch it
# shuffles and splits the training data into ``num_shards``.

###############################################################################
# Feed the pipeline into trainers
# -----------------------------------
# Let’s also implement a ``TrainingWorker`` which consumes the shuffled data
# from each shard.
#
# For simplicity, we will define a
# `Ray Actor <https://docs.ray.io/en/latest/actors.html>`_ that emulates
# training workers. Specifically,
#
# 1. It takes one shard of the shuffle pipeline for training;
# 2. It iterates over the shard to get a training dataset per epoch;
# 3. It then consumes the dataset by batches;


@ray.remote
class TrainingWorker:
    def __init__(self, rank: int, shard: DatasetPipeline):
        self.rank = rank
        self.shard = shard

    def train(self):
        for epoch, training_dataset in enumerate(self.shard.iter_datasets()):
            # Following code emulates epoch based SGD training.
            print(f"Training... worker: {self.rank}, epoch: {epoch}")
            for i, batch in enumerate(
                    training_dataset.iter_batches(batch_size=25000)):
                # TODO: replace the code for real training.
                pass


###########################################################################
# Let's run it
# -----------------------------
#
# Now let’s run the data pipeline end-to-end:
#
# First, let's parse some arguments.

parser = argparse.ArgumentParser()
parser.add_argument(
    "--large-scale-test",
    action="store_true",
    help="Run large scale test (500GiB of data).")

args, _ = parser.parse_known_args()

###############################################################################
#
# After that, let's generate 100MiB of Parquet files,
# create the shuffle pipeline by reading those generated Parquet files,
# and use training workers to consume the pipeline.

if not args.large_scale_test:

    NUM_TRAINING_WORKERS = 4
    NUM_EPOCHS = 5
    NUM_COLUMNS = 10
    SIZE_100MiB = 100 * 1024 * 1024

    # create a local ray cluster.
    ray.init()

    def generate_example_files(size_bytes: int) -> str:
        tmpdir = tempfile.mkdtemp()
        ray.data.read_datasource(
            RandomIntRowDatasource(),
            n=size_bytes // 8 // NUM_COLUMNS,
            num_columns=NUM_COLUMNS).write_parquet(tmpdir)
        return tmpdir

    example_files_dir = generate_example_files(SIZE_100MiB)

    splits = create_shuffle_pipeline(example_files_dir, NUM_EPOCHS,
                                     NUM_TRAINING_WORKERS)

    training_workers = [
        TrainingWorker.remote(rank, shard) for rank, shard in enumerate(splits)
    ]

    # Let's run the e2e pipeline
    start = time.time()
    ray.get([worker.train.remote() for worker in training_workers])
    print(f"total ingestion time: {time.time() - start}")

    # -> Write Progress: 100%|████████████████████| 201/201 [00:00<00:00, 228.67it/s]
    # -> Stage 0:   0%|          | 0/5 [00:00<?, ?it/s]
    # -> Stage 0:  40%|████      | 2/5 [00:11<00:17,  5.75s/it]
    # -> Stage 0:  60%|██████    | 3/5 [00:23<00:16,  8.15s/it]
    # -> ...
    # -> (TrainingWorker pid=1651600) Training... worker: 2, epoch: 0
    # -> Stage 0:  80%|████████  | 4/5 [00:35<00:09,  9.59s/it]
    # -> ...
    # -> (TrainingWorker pid=1651599) Training... worker: 0, epoch: 1
    # -> Stage 0: 100%|██████████| 5/5 [00:46<00:00, 10.34s/it]
    # -> ...
    # -> (TrainingWorker pid=1651387) Training... worker: 3, epoch: 4
    # -> total ingestion time: 61.0583393573761

#################################################################################
# Scale the shuffle ingestion pipeline
# --------------------------------------------------------
#
# Scaling the shuffle ingestion pipeline is simple. With Ray, we can linearly
# scale the pipeline from ingesting 100MiB of data to 500GiB of data by adding
# more machines.
#
# To ingest 500GiB of data, we'll set up a Ray Cluster.
# The provided :download:`big_data_ingestion.yaml <../big_data_ingestion.yaml>`
# cluster config can be used to set up an AWS cluster with 70 CPU nodes and
# 4 GPU nodes. Using following command to bring up the Ray cluster.
#
# .. code-block:: bash
#
#     $ pip install ray boto3
#     $ ray up big_data_ingestion.yaml
#
# After the cluster is started, let's implement our large scale ingestion test:
#
# First, since we are runing on a cluster, let's create the pipeline from
# RandomIntRowDatasource directly. In this way we don't need to set up S3 for storing
# generated data.


def create_large_shuffle_pipeline(data_size_bytes: int, num_epochs: int,
                                  num_columns: int,
                                  num_shards: int) -> List[DatasetPipeline]:
    # _spread_resource_prefix is used to ensure tasks are evenly spread to all
    # CPU nodes.
    return ray.data.read_datasource(
            RandomIntRowDatasource(), n=data_size_bytes // 8 // num_columns,
            num_columns=num_columns,
            _spread_resource_prefix="node:") \
        .repeat(num_epochs) \
        .random_shuffle_each_window(_spread_resource_prefix="node:") \
        .split(num_shards, equal=True)


#################################################################################
#
# Now, it's time to implement the 500GiB shuffle ingestion pipeline.

if args.large_scale_test:
    NUM_TRAINING_WORKERS = 16
    NUM_EPOCHS = 5
    NUM_COLUMNS = 10
    SIZE_500GiB = 500 * 1024 * 1024 * 1024

    # use the AWS cluster we just set up.
    ray.init(address="auto")

    splits = create_large_shuffle_pipeline(SIZE_500GiB, NUM_EPOCHS,
                                           NUM_COLUMNS,
                                           NUM_TRAINING_WORKERS)

    # Note we set num_gpus=1 for workers so that
    # the workers will only run on GPU nodes.
    training_workers = [
        TrainingWorker.options(num_gpus=1) \
            .remote(rank, shard) for rank, shard in enumerate(splits)
    ]

    start = time.time()

    # Let's run the large scale test.
    ray.get([
        worker.train.remote()
        for worker in training_workers
    ])
    print(f"total ingestion time: {time.time() - start}")

#################################################################################
#
# Finally, let's run our pipeline on the cluster we just started:
#
# .. code-block:: bash
#
#     $ ray submit ./big_data_ingestion.yaml ./big_data_ingestion.py --large-scale-test
