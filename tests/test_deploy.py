import unittest
from unittest.mock import patch
import launch

class VolumeTests(unittest.TestCase):
    def test_missing_volume_blocks_railway_start(self):
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID': 'test', 'DATA_DIR': '/data'}, clear=True):
            with self.assertRaises(RuntimeError):
                launch.check_volume()

    def test_unmounted_directory_blocks_start(self):
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID': 'test', 'DATA_DIR': '/data',
                                      'RAILWAY_VOLUME_MOUNT_PATH': '/data'}, clear=True), \
                patch('os.path.ismount', return_value=False):
            with self.assertRaises(RuntimeError):
                launch.check_volume()

    def test_correct_volume_allows_start(self):
        with patch.dict('os.environ', {'RAILWAY_PROJECT_ID': 'test', 'DATA_DIR': '/data',
                                      'RAILWAY_VOLUME_MOUNT_PATH': '/data'}, clear=True), \
                patch('os.path.ismount', return_value=True):
            launch.check_volume()
