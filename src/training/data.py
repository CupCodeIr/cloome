import logging
import math
import os
import random

import braceexpand
import numpy as np
import pandas as pd
import torch
import torchvision.datasets as datasets
import webdataset as wds
from PIL import Image
from dataclasses import dataclass
from training.datasets import CellPainting
from torch.utils.data import Dataset, DataLoader, SubsetRandomSampler
from torch.utils.data.distributed import DistributedSampler

from clip.clip import tokenize


class CsvDataset(Dataset):
    def __init__(self, input_filename, transforms, img_key, caption_key, img_folder, sep="\t"):
        logging.debug(f'Loading csv data from {input_filename}.')
        df = pd.read_csv(input_filename, sep=sep)

        self.images = df[img_key].tolist()
        self.images = [os.path.join(img_folder, img_name) for img_name in self.images]
        self.captions = df[caption_key].tolist()
        self.transforms = transforms
        logging.debug('Done loading data.')

    def __len__(self):
        return len(self.captions)

    def __getitem__(self, idx):
        images = self.transforms(Image.open(str(self.images[idx])))
        texts = tokenize([str(self.captions[idx])])[0]
        return images, texts


@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler


def get_cellpainting_dataset(args, preprocess_fn, is_train):
    input_index = args.train_index if is_train else args.val_index
    input_filename_mols = args.data_mols
    input_filename_imgs = args.image_path

    assert input_filename_mols
    assert input_filename_imgs

    dataset = CellPainting(
        input_index,
        input_filename_imgs,
        input_filename_mols,
        transforms=preprocess_fn
        )

    if args.debug_run:
        dataset = get_data_subset(dataset)

    num_samples = len(dataset)
    sampler = DistributedSampler(dataset, seed=args.seed) if args.distributed and is_train else None
    shuffle = is_train and sampler is None
    batch_size = args.batch_size if is_train else args.batch_size_eval

    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=args.workers,
        pin_memory=True,
        sampler=sampler,
        drop_last=is_train
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


def preprocess_txt(text):
    return tokenize([str(text)])[0]


def get_dataset_size(shards):
    shards_list = list(braceexpand.braceexpand(shards))
    dir_path = os.path.dirname(shards)
    sizes = eval(open(os.path.join(dir_path, 'sizes.json'), 'r').read())
    total_size = sum(
        [int(sizes[os.path.basename(shard)]) for shard in shards_list])
    num_shards = len(shards_list)
    return total_size, num_shards


def get_imagenet(args, preprocess_fns, split):
    assert split in ["train", "val", "v2"]
    is_train = split == "train"
    preprocess_train, preprocess_val = preprocess_fns

    if split == "v2":
        from imagenetv2_pytorch import ImageNetV2Dataset
        dataset = ImageNetV2Dataset(location=args.imagenet_v2, transform=preprocess_val)
    else:
        if is_train:
            data_path = args.imagenet_train
            preprocess_fn = preprocess_train
        else:
            data_path = args.imagenet_val
            preprocess_fn = preprocess_val
        assert data_path

        dataset = datasets.ImageFolder(data_path, transform=preprocess_fn)

    if args.debug_run:
        dataset = get_data_subset(dataset)

    if is_train:
        idxs = np.zeros(len(dataset.targets))
        target_array = np.array(dataset.targets)
        k = 50
        for c in range(1000):
            m = target_array == c
            n = len(idxs[m])
            arr = np.zeros(n)
            arr[:k] = 1
            np.random.shuffle(arr)
            idxs[m] = arr

        idxs = idxs.astype('int')
        sampler = SubsetRandomSampler(np.where(idxs)[0])
    else:
        sampler = None

    dataloader = torch.utils.data.DataLoader(
        dataset,
        shuffle=True,
        batch_size=args.batch_size_eval,
        num_workers=args.workers,
        sampler=sampler,
    )

    return DataInfo(dataloader, sampler)


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def get_wds_dataset(args, preprocess_img, is_train, run_mode=None):
    input_shards = args.train_data if is_train else args.val_data
    assert input_shards is not None

    # The following code is adapted from https://github.com/tmbdev/webdataset-examples/blob/master/main-wds.py
    num_samples, num_shards = get_dataset_size(input_shards)
    batch_size = args.batch_size
    if is_train and args.distributed:
        max_shards_per_node = math.ceil(num_shards / args.world_size)
        num_samples = args.world_size * (num_samples * max_shards_per_node // num_shards)
        num_batches = num_samples // (args.batch_size * args.world_size)
        num_samples = num_batches * args.batch_size * args.world_size
    elif is_train and not args.distributed:
        num_batches = num_samples // args.batch_size
    else:
        num_batches = num_samples // args.batch_size_eval
        batch_size = args.batch_size_eval
    # Set seed again before shardlist (which includes the shuffling)
    torch.manual_seed(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    shardlist = wds.PytorchShardList(
        input_shards,
        epoch_shuffle=is_train,
        split_by_node=is_train  # NOTE: we do eval on a single gpu.
    )
    dataset = (
        wds.WebDataset(shardlist)
            .decode("pil")
            .rename(image="jpg;png", text="txt")
            .map_dict(image=preprocess_img, text=preprocess_txt)
            .to_tuple("image", "text")
            .batched(batch_size, partial=not is_train or not args.distributed)
    )

    if args.debug_run:
        dataset = get_data_subset(dataset)

    dataloader = wds.WebLoader(
        dataset, batch_size=None, shuffle=False, num_workers=args.workers,
    )
    if is_train and args.distributed:
        # With DDP, we need to make sure that all nodes get the same number of batches;
        # we do that by reusing a little bit of data.
        dataloader = dataloader.repeat(2).slice(num_batches)
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader, None)


def get_csv_dataset(args, preprocess_fn, is_train, run_mode=None):
    input_filename = args.train_data if is_train else args.val_data
    assert input_filename
    dataset = CsvDataset(
        input_filename,
        preprocess_fn,
        img_key=args.csv_img_key,
        caption_key=args.csv_caption_key,
        img_folder=args.path_data,
        sep=args.csv_separator)

    if args.debug_run:
        dataset = get_data_subset(dataset)
    num_samples = len(dataset)
    sampler = DistributedSampler(dataset, seed=args.seed) if args.distributed and is_train else None
    shuffle = sampler is None

    batch_size = args.batch_size if is_train else args.batch_size_eval

    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=shuffle,
        num_workers=args.workers,
        pin_memory=True,
        sampler=sampler,
        drop_last=is_train,
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


def get_dataset_fn(data_path, dataset_type):
    if dataset_type == "webdataset":
        return get_wds_dataset
    elif dataset_type == "csv":
        return get_csv_dataset
    elif dataset_type == "auto":
        ext = data_path.split('.')[-1]
        if ext in ['csv', 'tsv']:
            return get_csv_dataset
        elif ext in ['tar']:
            return get_wds_dataset
        else:
            raise ValueError(
                f"Tried to figure out dataset type, but failed for extention {ext}.")
    else:
        raise ValueError(f"Unsupported dataset type: {dataset_type}")


def get_data_subset(dataset, n_samples=1000):
    """returns randomly subsampled dataset. Use for debug purposes."""
    idcs = np.arange(len(dataset))
    n_samples = min(n_samples, len(dataset))
    np.random.shuffle(idcs)  # shuffles inplace
    new_idcs = idcs[:n_samples]
    return torch.utils.data.Subset(dataset, new_idcs)


def get_data(args, preprocess_fns):
    preprocess_train, preprocess_val = preprocess_fns
    data = {}

    if args.train_index:
        data["train"] = get_cellpainting_dataset(args, preprocess_train, is_train=True)
    if args.val_index:
        data["val"] = get_cellpainting_dataset(args, preprocess_val, is_train=False)

    if args.imagenet_val is not None:
        data["imagenet-val"] = get_imagenet(args, preprocess_fns, "val")
    if args.imagenet_v2 is not None:
        data["imagenet-v2"] = get_imagenet(args, preprocess_fns, "v2")

    return data


# def get_data(args, preprocess_fns):
#     preprocess_train, preprocess_val = preprocess_fns
#     data = {}
#
#     if args.train_data:
#         data["train"] = get_dataset_fn(args.train_data, args.dataset_type)(
#             args, preprocess_train, is_train=True)
#     if args.val_data:
#         data["val"] = get_dataset_fn(args.val_data, args.dataset_type)(
#             args, preprocess_val, is_train=False, run_mode='train')
#
#     if args.imagenet_val is not None:
#         data["imagenet-val"] = get_imagenet(args, preprocess_fns, "val")
#     if args.imagenet_v2 is not None:
#         data["imagenet-v2"] = get_imagenet(args, preprocess_fns, "v2")
#
#     if args.debug_run:
#         for dn in data.keys():
#             logging.info(f'truncated dataset {dn} to {len(data[dn].dataloader.dataset)}')
#     return data
