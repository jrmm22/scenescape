# SPDX-FileCopyrightText: (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import argparse
import torch
from pathlib import Path
from addict import Dict
from typing import List, Union, Optional
import h5py
from types import SimpleNamespace
import cv2
import numpy as np
from tqdm import tqdm
import pprint
import collections.abc as collections
import PIL.Image

from . import extractors, logger
from .utils.base_model import cached_load
from .utils.tools import map_tensor
from .utils.parsers import parse_image_lists
from .utils.io import read_image, list_h5_names, base64_to_image


confs = {
    "superpoint_aachen": {
        "output": "feats-superpoint-n4096-r1024",
        "model": {"name": "superpoint", "nms_radius": 3, "max_keypoints": 4096},
        "preprocessing": {"grayscale": True, "resize_max": 1024},
    },
    # Resize images to 1600px even if they are originally smaller.
    # Improves the keypoint localization if the images are of good quality.
    "superpoint_max": {
        "output": "feats-superpoint-n4096-rmax1600",
        "model": {"name": "superpoint", "nms_radius": 3, "max_keypoints": 4096},
        "preprocessing": {"grayscale": True, "resize_max": 1600, "resize_force": True},
    },
    "superpoint_inloc": {
        "output": "feats-superpoint-n4096-r1600",
        "model": {"name": "superpoint", "nms_radius": 4, "max_keypoints": 4096},
        "preprocessing": {"grayscale": True, "resize_max": 1600},
    },
    "sift": {
        "output": "feats-sift",
        "model": {"name": "dog"},
        "preprocessing": {"grayscale": True, "resize_max": 1600},
    },
    "sosnet": {
        "output": "feats-sosnet",
        "model": {"name": "dog", "descriptor": "sosnet"},
        "preprocessing": {"grayscale": True, "resize_max": 1600},
    },
    # Global descriptors
    "netvlad": {
        "output": "global-feats-netvlad",
        "model": {"name": "netvlad"},
        "preprocessing": {"resize_max": 1024},
    },
    "openibl": {
        "output": "global-feats-openibl",
        "model": {"name": "openibl"},
        "preprocessing": {"resize_max": 1024},
    },
}
"""A set of standard configurations that can be directly selected from the
command line using their name. Each is a dictionary with the following entries:
    - output: the name of the feature file that will be generated.
    - model: the model configuration, as passed to a feature extractor.
    - preprocessing: how to preprocess the images read from disk.
"""


def resize_image(image, size, interp):
  if interp.startswith("cv2_"):
    interp = getattr(cv2, "INTER_" + interp[len("cv2_"):].upper())
    h, w = image.shape[:2]
    if interp == cv2.INTER_AREA and (w < size[0] or h < size[1]):
      interp = cv2.INTER_LINEAR
    resized = cv2.resize(image, size, interpolation=interp)
  elif interp.startswith("pil_"):
    interp = getattr(PIL.Image, interp[len("pil_"):].upper())
    resized = PIL.Image.fromarray(image.astype(np.uint8))
    resized = resized.resize(size, resample=interp)
    resized = np.asarray(resized, dtype=image.dtype)
  else:
    raise ValueError(f"Unknown interpolation {interp}.")
  return resized


class ImageDataset(torch.utils.data.Dataset):
  default_conf = {
      "globs": ["*.jpg", "*.jpeg", "*.JPG"],
      "grayscale": False,
      "resize_max": None,
      "resize_force": False,
      "interpolation": "cv2_area",  # pil_linear is more accurate but slower
  }

  def __init__(self, root, conf, paths=None):
    self.conf = Dict(self.default_conf) | Dict(conf)
    self.root = root

    if paths is None:
      paths = []
      for g in self.conf.globs:
        paths += list(Path(root).glob("**/" + g))
      if len(paths) == 0:
        raise ValueError(f"Could not find any image in root: {root}.")
      paths = sorted(list(set(paths)))
      self.names = [i.relative_to(root).as_posix() for i in paths]
      logger.info(f"Found {len(self.names)} images in root {root}.")
    elif isinstance(paths, dict):  # SceneScape message
      self.root = (paths,)
      self.names = (paths["name"],)
    else:
      if isinstance(paths, (Path, str)):
        self.names = parse_image_lists(paths)
      elif isinstance(paths, collections.Iterable):
        self.names = [p.as_posix() if isinstance(p, Path) else p for p in paths]
      else:
        raise ValueError(f"Unknown format for path argument {paths}.")

      for name in self.names:
        if not (root / name).exists():
          raise ValueError(f"Image {name} does not exists in root: {root}.")

  def __getitem__(self, idx):
    name = self.names[idx]
    if isinstance(self.root, tuple):  # SceneScape message
      image = base64_to_image(
          self.root[idx]["image_data"],
          self.conf.grayscale,
          log_as=self.names[idx],
      )
    else:
      image = read_image(self.root / name, self.conf.grayscale)
    image = image.astype(np.float32)
    size = image.shape[:2][::-1]

    if self.conf.resize_max and (
        self.conf.resize_force or max(size) > self.conf.resize_max
    ):
      scale = self.conf.resize_max / max(size)
      size_new = tuple(int(round(x * scale)) for x in size)
      image = resize_image(image, size_new, self.conf.interpolation)

    if self.conf.grayscale:
      image = image[None]
    else:
      image = image.transpose((2, 0, 1))  # HxWxC to CxHxW
    image = image / 255.0

    data = {"name": name, "image": image, "original_size": np.array(size)}
    return data

  def __len__(self):
    return len(self.names)


@torch.no_grad()
def main(
    conf: Dict,
    image_dir: Path,
    export_dir: Optional[Path] = None,
    as_half: bool = True,
    image_list: Optional[Union[Path, List[str], Dict]] = None,
    feature_path: Optional[Path] = None,
    overwrite: bool = False,
) -> Path:
  logger.info(
      "Extracting local features with configuration:" f"\n{pprint.pformat(conf)}"
  )

  loader = ImageDataset(image_dir, conf["preprocessing"], image_list)
  loader = torch.utils.data.DataLoader(loader, num_workers=0)

  if feature_path is None:
    feature_path = Path(export_dir, conf["output"] + ".h5")
  feature_path.parent.mkdir(exist_ok=True, parents=True)
  skip_names = set(
      list_h5_names(feature_path) if feature_path.exists() and not overwrite else ()
  )
  if set(loader.dataset.names).issubset(set(skip_names)):
    logger.info("Skipping the extraction.")
    return feature_path

  model = cached_load(extractors, conf["model"])
  for data in tqdm(loader):
    name = data["name"][0]  # remove batch dimension
    if name in skip_names:
      continue

    pred = model(map_tensor(data, lambda x: x.to(cached_load.device)))
    pred = {k: v[0].cpu().numpy() for k, v in pred.items()}

    pred["image_size"] = original_size = data["original_size"][0].numpy()
    if "keypoints" in pred:
      size = np.array(data["image"].shape[-2:][::-1])
      scales = (original_size / size).astype(np.float32)
      pred["keypoints"] = (pred["keypoints"] + 0.5) * scales[None] - 0.5
      # add keypoint uncertainties scaled to the original resolution
      uncertainty = getattr(model, "detection_noise", 1) * scales.mean()

    if as_half:
      for k in pred:
        dt = pred[k].dtype
        if (dt == np.float32) and (dt != np.float16):
          pred[k] = pred[k].astype(np.float16)

    with h5py.File(str(feature_path), 'a', libver='latest') as fd:
      try:
        if name in fd:
          del fd[name]
        grp = fd.create_group(name)
        for k, v in pred.items():
          grp.create_dataset(k, data=v)
        if "keypoints" in pred:
          grp["keypoints"].attrs["uncertainty"] = uncertainty
      except OSError as error:
        if "No space left on device" in error.args[0]:
          logger.error(
              "Out of disk space: storing features on disk can take "
              "significant space, did you enable the as_half flag?"
          )
          del grp, fd[name]
        raise error

    del pred

  logger.info("Finished exporting features.")
  return feature_path


if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("--image_dir", type=Path, required=True)
  parser.add_argument("--export_dir", type=Path, required=True)
  parser.add_argument(
      "--conf", type=str, default="superpoint_aachen", choices=list(confs.keys())
  )
  parser.add_argument("--as_half", action="store_true")
  parser.add_argument("--image_list", type=Path)
  parser.add_argument("--feature_path", type=Path)
  args = parser.parse_args()
  main(confs[args.conf], args.image_dir, args.export_dir, args.as_half)
