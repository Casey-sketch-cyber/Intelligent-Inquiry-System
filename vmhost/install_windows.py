"""仅为本项目的全新空 VM 准备 Windows PE 安装；账号与隐私设置留给用户。"""
from pathlib import Path
from xml.etree import ElementTree as ET

from inquiry.runtime import DATA_DIR, require_d_path
from vmhost import manager


def validate_empty_machine(cfg):
    # Never power off or reset a running installer. Old boot-error logs are not
    # evidence that the current guest is safe to interrupt.
    if manager.info()['state'] != 'poweroff':
        raise RuntimeError('只允许为已关机的全新空虚拟机准备安装媒体')
    folder = require_d_path(Path(cfg['folder']))
    attached = manager.parse_info(manager.command('showvminfo', cfg['uuid'], '--machinereadable'))
    for port, name in enumerate(('system.vdi', 'data.vdi')):
        disk = require_d_path(folder / name)
        actual = require_d_path(Path(attached.get(f'SATA-{port}-0', '')))
        if actual != disk:
            raise RuntimeError('虚拟机挂载磁盘与专用磁盘不符，拒绝安装')
        if disk.stat().st_size != 2097152:
            raise RuntimeError('磁盘已写入内容，拒绝自动重装')
    iso = require_d_path(Path(cfg.get('iso') or ''))
    if not iso.is_file() or iso.suffix.lower() != '.iso':
        raise RuntimeError('请先挂载已校验的 D 盘 Windows ISO')


def write_template(template):
    ns = 'urn:schemas-microsoft-com:unattend'
    wcm = 'http://schemas.microsoft.com/WMIConfig/2002/State'
    ET.register_namespace('', ns)
    ET.register_namespace('wcm', wcm)

    def add(parent, tag, text=None, **attrs):
        element = ET.SubElement(parent, '{' + ns + '}' + tag, attrs)
        if text is not None:
            element.text = str(text)
        return element

    root = ET.Element('{' + ns + '}unattend')
    settings = add(root, 'settings', **{'pass': 'windowsPE'})
    attrs = dict(processorArchitecture='amd64', publicKeyToken='31bf3856ad364e35',
                 language='neutral', versionScope='nonSxS')
    international = add(settings, 'component', name='Microsoft-Windows-International-Core-WinPE', **attrs)
    add(add(international, 'SetupUILanguage'), 'UILanguage', 'zh-CN')
    for key in ('InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale'):
        add(international, key, 'zh-CN')
    setup = add(settings, 'component', name='Microsoft-Windows-Setup', **attrs)
    diskconfig = add(setup, 'DiskConfiguration')
    add(diskconfig, 'WillShowUI', 'OnError')
    action = {'{' + wcm + '}action': 'add'}
    disk = add(diskconfig, 'Disk', **action)
    add(disk, 'DiskID', 0)
    add(disk, 'WillWipeDisk', 'true')
    partitions = add(disk, 'CreatePartitions')
    for order, kind, size in [(1, 'EFI', 260), (2, 'MSR', 16), (3, 'Primary', 64230), (4, 'Primary', None)]:
        part = add(partitions, 'CreatePartition', **action)
        add(part, 'Order', order)
        add(part, 'Type', kind)
        add(part, 'Size', size) if size else add(part, 'Extend', 'true')
    modifications = add(disk, 'ModifyPartitions')
    for order, part_id, kind, label, letter in [(1, 1, 'FAT32', 'System', None),
                                               (2, 3, 'NTFS', 'Windows', 'C'),
                                               (3, 4, 'NTFS', 'Recovery', None)]:
        part = add(modifications, 'ModifyPartition', **action)
        for key, value in [('Order', order), ('PartitionID', part_id), ('Format', kind), ('Label', label)]:
            add(part, key, value)
        if letter:
            add(part, 'Letter', letter)
        if label == 'Recovery':
            add(part, 'TypeID', 'de94bba4-06d1-4d40-a16a-bfd50179d6ac')
    add(add(setup, 'UserData'), 'AcceptEula', 'true')
    osimage = add(add(setup, 'ImageInstall'), 'OSImage')
    metadata = add(add(osimage, 'InstallFrom'), 'MetaData', **action)
    add(metadata, 'Key', '/IMAGE/INDEX')
    add(metadata, 'Value', 1)  # 已核实本次简体中文多版本 ISO 的第 1 项为家庭版。
    target = add(osimage, 'InstallTo')
    add(target, 'DiskID', 0)
    add(target, 'PartitionID', 3)
    add(osimage, 'WillShowUI', 'OnError')
    ET.indent(root)
    ET.ElementTree(root).write(template, encoding='utf-8', xml_declaration=True)


def prepare():
    cfg = manager.read_config()
    validate_empty_machine(cfg)
    base = require_d_path(DATA_DIR / 'vm/unattended')
    base.mkdir(exist_ok=True)
    template = base / 'windows-pe.xml'
    write_template(template)
    post = base / 'no-postinstall.cmd'
    post.write_text('@echo off\nexit /b 0\n', encoding='ascii')
    # VirtualBox 的准备接口要求此参数；自定义 XML 不引用它，不创建账户或自动登录。
    import secrets
    password = base / 'unused-password.txt'
    password.write_text(secrets.token_urlsafe(32), encoding='ascii')
    try:
        result = manager.command('unattended', 'install', cfg['uuid'], '--iso=' + cfg['iso'],
            '--image-index=1', '--locale=zh_CN', '--language=zh-CN', '--country=CN',
            '--no-install-additions', '--auxiliary-base-path=' + str(base / 'tianxin-'),
            '--user-password-file=' + str(password), '--script-template=' + str(template),
            '--post-install-template=' + str(post), timeout=180)
    finally:
        password.unlink(missing_ok=True)
    (base / 'prepare.log').write_text(result, encoding='utf-8')
    print('已准备 Windows 安装媒体；未配置账号、自动登录或隐私选项。')


if __name__ == '__main__':
    prepare()
