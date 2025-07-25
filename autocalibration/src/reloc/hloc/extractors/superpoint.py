# SPDX-FileCopyrightText: (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

import sys
from pathlib import Path
import torch

from ..utils.base_model import BaseModel

sys.path.append(str(Path(__file__).parent / '../../third_party'))
from SuperGluePretrainedNetwork.models import superpoint  # noqa E402


# torch>=1.10 breaks the original version check.
# We monkeypatch the function until this PR is merged:
# https://github.com/magicleap/SuperGluePretrainedNetwork/pull/104
def sample_descriptors(keypoints, descriptors, s: int = 8):
  """ Interpolate descriptors at keypoint locations """
  b, c, h, w = descriptors.shape
  keypoints = keypoints - s / 2 + 0.5
  keypoints /= torch.tensor([(w*s - s/2 - 0.5), (h*s - s/2 - 0.5)],
                            ).to(keypoints)[None]
  keypoints = keypoints*2 - 1  # normalize to (-1, 1)
  args = {'align_corners': True} if torch.__version__ >= '1.3' else {}
  descriptors = torch.nn.functional.grid_sample(
      descriptors, keypoints.view(b, 1, -1, 2), mode='bilinear', **args)
  descriptors = torch.nn.functional.normalize(
      descriptors.reshape(b, c, -1), p=2, dim=1)
  return descriptors


superpoint.sample_descriptors = sample_descriptors


# The original keypoint sampling is incorrect. We patch it here but
# we don't fix it upstream to not impact exisiting evaluations.
def sample_descriptors_fix_sampling(keypoints, descriptors, s: int = 8):
  """ Interpolate descriptors at keypoint locations """
  b, c, h, w = descriptors.shape
  keypoints = (keypoints + 0.5) / (keypoints.new_tensor([w, h]) * s)
  keypoints = keypoints * 2 - 1  # normalize to (-1, 1)
  descriptors = torch.nn.functional.grid_sample(
      descriptors, keypoints.view(b, 1, -1, 2),
      mode='bilinear', align_corners=False)
  descriptors = torch.nn.functional.normalize(
      descriptors.reshape(b, c, -1), p=2, dim=1)
  return descriptors


class SuperPoint(BaseModel):
  default_conf = {
      'nms_radius': 4,
      'keypoint_threshold': 0.005,
      'max_keypoints': -1,
      'remove_borders': 4,
      'fix_sampling': False,
  }
  required_inputs = ['image']
  detection_noise = 2.0

  def _init(self, conf):
    if conf['fix_sampling']:
      superpoint.sample_descriptors = sample_descriptors_fix_sampling
    self.net = superpoint.SuperPoint(conf)

  def _forward(self, data):
    return self.net(data)
