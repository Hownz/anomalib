"""Anomalib datamodule base class."""

# Copyright (C) 2022 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import logging
from abc import ABC
from typing import TYPE_CHECKING, Any

from lightning.pytorch import LightningDataModule
from lightning.pytorch.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from torch.utils.data.dataloader import DataLoader, default_collate

from anomalib.data.utils import TestSplitMode, ValSplitMode, random_split, split_by_label
from anomalib.data.utils.synthetic import SyntheticAnomalyDataset

if TYPE_CHECKING:
    from pandas import DataFrame

    from anomalib.data.base.dataset import AnomalibDataset

logger = logging.getLogger(__name__)


def collate_fn(batch: list) -> dict[str, Any]:
    """Collate bounding boxes as lists.

    Bounding boxes are collated as a list of tensors, while the default collate function is used for all other entries.

    Args:
        batch (List): list of items in the batch where len(batch) is equal to the batch size.

    Returns:
        dict[str, Any]: Dictionary containing the collated batch information.
    """
    elem = batch[0]  # sample an element from the batch to check the type.
    out_dict = {}
    if isinstance(elem, dict):
        if "boxes" in elem:
            # collate boxes as list
            out_dict["boxes"] = [item.pop("boxes") for item in batch]
        # collate other data normally
        out_dict.update({key: default_collate([item[key] for item in batch]) for key in elem})
        return out_dict
    return default_collate(batch)


class AnomalibDataModule(LightningDataModule, ABC):
    """Base Anomalib data module.

    Args:
        train_batch_size (int): Batch size used by the train dataloader.
        eval_batch_size (int): Batch size used by the val and test dataloaders.
        num_workers (int): Number of workers used by the train, val and test dataloaders.
        val_split_mode (ValSplitMode): Determines how the validation split is obtained.
            Options: [none, same_as_test, from_test, synthetic]
        val_split_ratio (float): Fraction of the train or test images held our for validation.
        test_split_mode (Optional[TestSplitMode], optional): Determines how the test split is obtained.
            Options: [none, from_dir, synthetic].
            Defaults to ``None``.
        test_split_ratio (float): Fraction of the train images held out for testing.
            Defaults to ``None``.
        seed (int | None, optional): Seed used during random subset splitting.
            Defaults to ``None``.
    """

    def __init__(
        self,
        train_batch_size: int,
        eval_batch_size: int,
        num_workers: int,
        val_split_mode: ValSplitMode | str,
        val_split_ratio: float,
        test_split_mode: TestSplitMode | str | None = None,
        test_split_ratio: float | None = None,
        seed: int | None = None,
    ) -> None:
        super().__init__()
        self.train_batch_size = train_batch_size
        self.eval_batch_size = eval_batch_size
        self.num_workers = num_workers
        self.test_split_mode = TestSplitMode(test_split_mode) if test_split_mode else TestSplitMode.NONE
        self.test_split_ratio = test_split_ratio
        self.val_split_mode = ValSplitMode(val_split_mode)
        self.val_split_ratio = val_split_ratio
        self.seed = seed

        self.train_data: AnomalibDataset
        self.val_data: AnomalibDataset
        self.test_data: AnomalibDataset

        self._samples: DataFrame | None = None

    def setup(self, stage: str | None = None) -> None:
        """Set up train, validation and test data.

        Args:
            stage: str | None:  Train/Val/Test stages.
                Defaults to ``None``.
        """
        if not self.is_setup:
            self._setup(stage)
        assert self.is_setup

    def _setup(self, _stage: str | None = None) -> None:
        """Set up the datasets and perform dynamic subset splitting.

        This method may be overridden in subclass for custom splitting behaviour.

        Note:
            The stage argument is not used here. This is because, for a given instance of an AnomalibDataModule
            subclass, all three subsets are created at the first call of setup(). This is to accommodate the subset
            splitting behaviour of anomaly tasks, where the validation set is usually extracted from the test set, and
            the test set must therefore be created as early as the `fit` stage.

        """
        assert self.train_data is not None
        assert self.test_data is not None

        self.train_data.setup()
        self.test_data.setup()

        self._create_test_split()
        self._create_val_split()

    def _create_test_split(self) -> None:
        """Obtain the test set based on the settings in the config."""
        if self.test_data.has_normal:
            # split the test data into normal and anomalous so these can be processed separately
            normal_test_data, self.test_data = split_by_label(self.test_data)
        elif self.test_split_mode != TestSplitMode.NONE:
            # when the user did not provide any normal images for testing, we sample some from the training set,
            # except when the user explicitly requested no test splitting.
            logger.info(
                "No normal test images found. Sampling from training set using a split ratio of %0.2f",
                self.test_split_ratio,
            )
            if self.test_split_ratio is not None:
                self.train_data, normal_test_data = random_split(self.train_data, self.test_split_ratio, seed=self.seed)

        if self.test_split_mode == TestSplitMode.FROM_DIR:
            self.test_data += normal_test_data
        elif self.test_split_mode == TestSplitMode.SYNTHETIC:
            self.test_data = SyntheticAnomalyDataset.from_dataset(normal_test_data)
        elif self.test_split_mode != TestSplitMode.NONE:
            msg = f"Unsupported Test Split Mode: {self.test_split_mode}"
            raise ValueError(msg)

    def _create_val_split(self) -> None:
        """Obtain the validation set based on the settings in the config."""
        if self.val_split_mode == ValSplitMode.FROM_TEST:
            # randomly sampled from test set
            self.test_data, self.val_data = random_split(
                self.test_data,
                self.val_split_ratio,
                label_aware=True,
                seed=self.seed,
            )
        elif self.val_split_mode == ValSplitMode.SAME_AS_TEST:
            # equal to test set
            self.val_data = self.test_data
        elif self.val_split_mode == ValSplitMode.SYNTHETIC:
            # converted from random training sample
            self.train_data, normal_val_data = random_split(self.train_data, self.val_split_ratio, seed=self.seed)
            self.val_data = SyntheticAnomalyDataset.from_dataset(normal_val_data)
        elif self.val_split_mode != ValSplitMode.NONE:
            msg = f"Unknown validation split mode: {self.val_split_mode}"
            raise ValueError(msg)

    @property
    def is_setup(self) -> bool:
        """Checks if setup() has been called.

        At least one of [train_data, val_data, test_data] should be setup.
        """
        _is_setup: bool = False
        for data in ("train_data", "val_data", "test_data"):
            if hasattr(self, data) and getattr(self, data).is_setup:
                _is_setup = True

        return _is_setup

    def train_dataloader(self) -> TRAIN_DATALOADERS:
        """Get train dataloader."""
        return DataLoader(
            dataset=self.train_data,
            shuffle=True,
            batch_size=self.train_batch_size,
            num_workers=self.num_workers,
        )

    def val_dataloader(self) -> EVAL_DATALOADERS:
        """Get validation dataloader."""
        return DataLoader(
            dataset=self.val_data,
            shuffle=False,
            batch_size=self.eval_batch_size,
            num_workers=self.num_workers,
            collate_fn=collate_fn,
        )

    def test_dataloader(self) -> EVAL_DATALOADERS:
        """Get test dataloader."""
        return DataLoader(
            dataset=self.test_data,
            shuffle=False,
            batch_size=self.eval_batch_size,
            num_workers=self.num_workers,
            collate_fn=collate_fn,
        )

    def predict_dataloader(self) -> EVAL_DATALOADERS:
        """Use the test dataloader for inference unless overridden."""
        return self.test_dataloader()
