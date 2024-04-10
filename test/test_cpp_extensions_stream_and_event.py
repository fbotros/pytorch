# Owner(s): ["module: unknown"]

import os
import shutil
import sys
import tempfile
import unittest

import torch
import torch.testing._internal.common_utils as common
import torch.utils.cpp_extension
from torch.testing._internal.common_utils import (
    IS_ARM64,
    IS_LINUX,
    TEST_CUDA,
    TEST_PRIVATEUSE1,
)
from torch.utils.cpp_extension import CUDA_HOME, ROCM_HOME


TEST_CUDA = TEST_CUDA and CUDA_HOME is not None
TEST_ROCM = TEST_CUDA and torch.version.hip is not None and ROCM_HOME is not None


def remove_build_path():
    if sys.platform == "win32":
        # Not wiping extensions build folder because Windows
        return
    default_build_root = torch.utils.cpp_extension.get_default_build_root()
    if os.path.exists(default_build_root):
        shutil.rmtree(default_build_root, ignore_errors=True)


# Since we use a fake MTIA device backend to test generic Stream/Event, device backends are mutual exclusive to each other.
# The test will be skipped if any of the following conditions are met:
@unittest.skipIf(
    IS_ARM64 or not IS_LINUX or TEST_CUDA or TEST_PRIVATEUSE1,
    "Only on linux platform and mutual exclusive to other backends",
)
@torch.testing._internal.common_utils.markDynamoStrictTest
class TestCppExtensionStreamAndEvent(common.TestCase):
    """Tests Stream and Event with C++ extensions."""

    module = None

    def setUp(self):
        super().setUp()
        # cpp extensions use relative paths. Those paths are relative to
        # this file, so we'll change the working directory temporarily
        self.old_working_dir = os.getcwd()
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

    def tearDown(self):
        super().tearDown()
        # return the working directory (see setUp)
        os.chdir(self.old_working_dir)

    @classmethod
    def tearDownClass(cls):
        remove_build_path()

    @classmethod
    def setUpClass(cls):
        remove_build_path()
        build_dir = tempfile.mkdtemp()
        # Load the fake device guard impl.
        cls.module = torch.utils.cpp_extension.load(
            name="mtia_extension",
            sources=["cpp_extensions/mtia_extension.cpp"],
            build_directory=build_dir,
            extra_include_paths=[
                "cpp_extensions",
                "path / with spaces in it",
                "path with quote'",
            ],
            is_python_module=False,
            verbose=True,
        )

    def test_stream_event(self):
        s = torch.Stream()
        self.assertTrue(s.device_type, int(torch._C._autograd.DeviceType.MTIA))
        e = torch.Event()
        self.assertTrue(e.device.type, "mtia")
        # Should be nullptr by default
        self.assertTrue(e.event_id == 0)
        s.record_event(event=e)
        print(f"recorded event 1: {e}")
        self.assertTrue(e.event_id != 0)
        e2 = s.record_event()
        print(f"recorded event 2: {e2}")
        self.assertTrue(e2.event_id != 0)
        self.assertTrue(e2.event_id != e.event_id)
        e.synchronize()
        e2.synchronize()
        time_elapsed = e.elapsed_time(e2)
        print(f"time elapsed between e1 and e2: {time_elapsed}")
        old_event_id = e.event_id
        e.record(stream=s)
        print(f"recorded event 1: {e}")
        self.assertTrue(e.event_id == old_event_id)


if __name__ == "__main__":
    common.run_tests()
