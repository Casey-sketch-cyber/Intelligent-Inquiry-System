"""Installation preparation must never reset a running or previously used VM."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from inquiry.runtime import TEMP_DIR
from vmhost import install_windows as installer


class WindowsInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_DIR, prefix='windows-install-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('system.vdi', 'data.vdi'):
            with (self.root / name).open('wb') as out:
                out.truncate(2097152)
        self.iso = self.root / 'windows.iso'
        self.iso.touch()
        self.cfg = {'folder': str(self.root), 'uuid': 'test', 'iso': str(self.iso)}
        self.attachments = '\n'.join(
            f'"SATA-{port}-0"="{self.root / name}"'
            for port, name in enumerate(('system.vdi', 'data.vdi'))
        )

    def test_running_or_saved_vm_is_never_powered_off(self):
        for state in ('running', 'paused', 'saved'):
            with self.subTest(state=state), patch.object(installer.manager, 'info', return_value={'state': state}), \
                    patch.object(installer.manager, 'command') as command:
                with self.assertRaises(RuntimeError):
                    installer.validate_empty_machine(self.cfg)
                command.assert_not_called()

    def test_written_disk_and_other_attachment_are_rejected(self):
        with patch.object(installer.manager, 'info', return_value={'state': 'poweroff'}), \
                patch.object(installer.manager, 'command', return_value=self.attachments):
            installer.validate_empty_machine(self.cfg)
            with (self.root / 'data.vdi').open('ab') as out:
                out.write(b'written')
            with self.assertRaisesRegex(RuntimeError, '磁盘已写入'):
                installer.validate_empty_machine(self.cfg)
        other = self.attachments.replace('system.vdi', 'other.vdi')
        with patch.object(installer.manager, 'info', return_value={'state': 'poweroff'}), \
                patch.object(installer.manager, 'command', return_value=other):
            with self.assertRaisesRegex(RuntimeError, '挂载磁盘'):
                installer.validate_empty_machine(self.cfg)

    def test_answer_file_only_installs_disk_zero_and_leaves_user_setup_manual(self):
        target = self.root / 'answer.xml'
        installer.write_template(target)
        root = ET.parse(target).getroot()
        ns = {'u': 'urn:schemas-microsoft-com:unattend'}
        self.assertEqual([x.attrib['pass'] for x in root.findall('u:settings', ns)], ['windowsPE'])
        self.assertEqual([x.text for x in root.findall('.//u:DiskID', ns)], ['0', '0'])
        self.assertEqual(root.find('.//u:InstallTo/u:PartitionID', ns).text, '3')
        tags = {x.tag.rsplit('}', 1)[-1] for x in root.iter()}
        self.assertTrue(tags.isdisjoint({'AutoLogon', 'UserAccounts', 'OOBE', 'RunSynchronous', 'ProductKey'}))
        self.assertNotIn('Bypass', target.read_text(encoding='utf-8'))
