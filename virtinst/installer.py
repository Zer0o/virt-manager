#
# Common code for all guests
#
# Copyright 2006-2009, 2013 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import os
import logging

import libvirt

from .devices import DeviceDisk
from .domain import DomainOs
from .osdict import OSDB
from .installertreemedia import InstallerTreeMedia
from . import util


class Installer(object):
    """
    Class for kicking off VM installs. The VM is set up separately in a Guest
    instance. This class tracks the install media/bootdev choice, alters the
    Guest XML, boots it for the install, then saves the post install XML
    config. The Guest is passed in via start_install, only install media
    selection is done at __init__ time

    :param cdrom: Path to a cdrom device or iso. Maps to virt-install --cdrom
    :param location: An install tree URI, local directory, or ISO/CDROM path.
        Largely handled by installtreemedia helper class. Maps to virt-install
        --location
    :param install_bootdev: The VM bootdev to use (HD, NETWORK, CDROM, FLOPPY)
    :param location_kernel: URL pointing to a kernel to fetch, or a relative
        path to indicate where the kernel is stored in location
    :param location_initrd: location_kernel, but pointing to an initrd
    """
    def __init__(self, conn, cdrom=None, location=None, install_bootdev=None,
            location_kernel=None, location_initrd=None):
        self.conn = conn

        self.livecd = False
        self.extra_args = []

        # Entry point for virt-manager 'Customize' wizard to change autostart
        self.autostart = False

        self._install_bootdev = install_bootdev
        self._install_kernel = None
        self._install_initrd = None
        self._install_cdrom_device = None
        self._defaults_are_set = False

        if location_kernel or location_initrd:
            if not location:
                raise ValueError(_("location kernel/initrd may only "
                    "be specified with a location URL/path"))
            if not (location_kernel and location_initrd):
                raise ValueError(_("location kernel/initrd must be "
                    "be specified as a pair"))

        self._cdrom = None
        self._treemedia = None
        if cdrom:
            cdrom = InstallerTreeMedia.validate_path(self.conn, cdrom)
            self._cdrom = cdrom
            self._install_bootdev = "cdrom"
        if location:
            self._treemedia = InstallerTreeMedia(self.conn, location,
                    location_kernel, location_initrd)


    ###################
    # Private helpers #
    ###################

    def _cdrom_path(self):
        if self._treemedia:
            return self._treemedia.cdrom_path()
        return self._cdrom

    def _add_install_cdrom_device(self, guest):
        if self._install_cdrom_device:
            return
        if not bool(self._cdrom_path()):
            return

        dev = DeviceDisk(self.conn)
        dev.device = dev.DEVICE_CDROM
        dev.path = self._cdrom_path()
        dev.sync_path_props()
        dev.validate()
        self._install_cdrom_device = dev

        # Insert the CDROM before any other CDROM, so boot=cdrom picks
        # it as the priority
        for idx, disk in enumerate(guest.devices.disk):
            if disk.is_cdrom():
                guest.devices.add_child(self._install_cdrom_device, idx=idx)
                return
        guest.add_device(self._install_cdrom_device)

    def _remove_install_cdrom_media(self, guest):
        if not self._install_cdrom_device:
            return
        if self.livecd:
            return
        if guest.osinfo.is_windows():
            # Keep media attached for windows which has a multi stage install
            return
        self._install_cdrom_device.path = None
        self._install_cdrom_device.sync_path_props()

    def _build_boot_order(self, guest, bootdev):
        bootorder = [bootdev]

        # If guest has an attached disk, always have 'hd' in the boot
        # list, so disks are marked as bootable/installable (needed for
        # windows virtio installs, and booting local disk from PXE)
        for disk in guest.devices.disk:
            if disk.device == disk.DEVICE_DISK:
                bootdev = "hd"
                if bootdev not in bootorder:
                    bootorder.append(bootdev)
                break
        return bootorder

    def _can_set_guest_bootorder(self, guest):
        return (not guest.os.is_container() and
            not guest.os.kernel and
            not any([d.boot.order for d in guest.devices.get_all()]))

    def _alter_bootconfig(self, guest):
        """
        Generate the portion of the guest xml that determines boot devices
        and parameters. (typically the <os></os> block)

        :param guest: Guest instance we are installing
        """
        guest.on_reboot = "destroy"

        if self._install_kernel:
            guest.os.kernel = self._install_kernel
        if self._install_initrd:
            guest.os.initrd = self._install_initrd
        if self.extra_args:
            guest.os.kernel_args = " ".join(self.extra_args)

        bootdev = self._install_bootdev
        if bootdev and self._can_set_guest_bootorder(guest):
            guest.os.bootorder = self._build_boot_order(guest, bootdev)
        else:
            guest.os.bootorder = []


    ##########################
    # Internal API overrides #
    ##########################

    def _prepare(self, guest, meter):
        if self._treemedia:
            k, i, a = self._treemedia.prepare(guest, meter)
            self._install_kernel = k
            self._install_initrd = i
            if a and "VIRTINST_INITRD_TEST" not in os.environ:
                self.extra_args.append(a)

    def _cleanup(self, guest):
        if self._treemedia:
            self._treemedia.cleanup(guest)

    def _get_postinstall_bootdev(self, guest):
        if self.cdrom and self.livecd:
            return DomainOs.BOOT_DEVICE_CDROM

        if self._install_bootdev:
            if any([d for d in guest.devices.disk
                    if d.device == d.DEVICE_DISK]):
                return DomainOs.BOOT_DEVICE_HARDDISK
            return self._install_bootdev

        device = guest.devices.disk and guest.devices.disk[0].device or None
        if device == DeviceDisk.DEVICE_DISK:
            return DomainOs.BOOT_DEVICE_HARDDISK
        elif device == DeviceDisk.DEVICE_CDROM:
            return DomainOs.BOOT_DEVICE_CDROM
        elif device == DeviceDisk.DEVICE_FLOPPY:
            return DomainOs.BOOT_DEVICE_FLOPPY
        return DomainOs.BOOT_DEVICE_HARDDISK


    ##############
    # Public API #
    ##############

    @property
    def location(self):
        if self._treemedia:
            return self._treemedia.location

    @property
    def cdrom(self):
        return self._cdrom

    def set_initrd_injections(self, initrd_injections):
        if self._treemedia:
            self._treemedia.initrd_injections = initrd_injections

    def set_install_defaults(self, guest):
        """
        Allow API users to set defaults ahead of time if they want it.
        Used by vmmDomainVirtinst so the 'Customize before install' dialog
        shows accurate values.

        If the user doesn't explicitly call this, it will be called by
        start_install()
        """
        if self._defaults_are_set:
            return

        self._add_install_cdrom_device(guest)

        if not guest.os.bootorder and self._can_set_guest_bootorder(guest):
            bootdev = self._get_postinstall_bootdev(guest)
            guest.os.bootorder = self._build_boot_order(guest, bootdev)

        guest.set_defaults(None)
        self._defaults_are_set = True

    def get_search_paths(self, guest):
        """
        Return a list of paths that the hypervisor will need search access
        for to perform this install.
        """
        search_paths = []
        if self._treemedia:
            search_paths.append(util.make_scratchdir(guest))
        if self._cdrom_path():
            search_paths.append(self._cdrom_path())
        return search_paths

    def has_install_phase(self):
        """
        Return True if the requested setup is actually installing an OS
        into the guest. Things like LiveCDs, Import, or a manually specified
        bootorder do not have an install phase.
        """
        if self.cdrom and self.livecd:
            return False
        return bool(self._cdrom or
                    self._install_bootdev or
                    self._treemedia)

    def detect_distro(self, guest):
        """
        Attempt to detect the distro for the Installer's 'location'. If
        an error is encountered in the detection process (or if detection
        is not relevant for the Installer type), None is returned.

        :returns: distro variant string, or None
        """
        ret = None
        if self._treemedia:
            ret = self._treemedia.detect_distro(guest)
        elif self.cdrom:
            if guest.conn.is_remote():
                logging.debug("Can't detect distro for cdrom "
                    "remote connection.")
            else:
                osguess = OSDB.guess_os_by_iso(self.cdrom)
                if osguess:
                    ret = osguess[0]
        else:
            logging.debug("No media for distro detection.")

        logging.debug("installer.detect_distro returned=%s", ret)
        return ret


    ##########################
    # guest install handling #
    ##########################

    def _prepare_get_install_xml(self, guest):
        # We do a shallow copy of the OS block here, so that we can
        # set the install time properties but not permanently overwrite
        # any config the user explicitly requested.
        data = (guest.os.bootorder, guest.os.kernel, guest.os.initrd,
                guest.os.kernel_args, guest.on_reboot)
        return data

    def _finish_get_install_xml(self, guest, data):
        (guest.os.bootorder, guest.os.kernel, guest.os.initrd,
                guest.os.kernel_args, guest.on_reboot) = data

    def _get_install_xml(self, guest):
        data = self._prepare_get_install_xml(guest)
        try:
            self._alter_bootconfig(guest)
            ret = guest.get_xml()
            return ret
        finally:
            self._remove_install_cdrom_media(guest)
            self._finish_get_install_xml(guest, data)

    def _build_xml(self, guest):
        install_xml = None
        if self.has_install_phase():
            install_xml = self._get_install_xml(guest)
        final_xml = guest.get_xml()

        logging.debug("Generated install XML: %s",
            (install_xml and ("\n" + install_xml) or "None required"))
        logging.debug("Generated boot XML: \n%s", final_xml)

        return install_xml, final_xml

    def _manual_transient_create(self, install_xml, final_xml, needs_boot):
        """
        For hypervisors (like vz) that don't implement createXML,
        we need to define+start, and undefine on start failure
        """
        domain = self.conn.defineXML(install_xml or final_xml)
        if not needs_boot:
            return domain

        # Handle undefining the VM if the initial startup fails
        try:
            domain.create()
        except Exception:
            try:
                domain.undefine()
            except Exception:
                pass
            raise

        if install_xml and install_xml != final_xml:
            domain = self.conn.defineXML(final_xml)
        return domain

    def _create_guest(self, guest,
                      meter, install_xml, final_xml, doboot, transient):
        """
        Actually do the XML logging, guest defining/creating

        :param doboot: Boot guest even if it has no install phase
        """
        meter_label = _("Creating domain...")
        meter = util.ensure_meter(meter)
        meter.start(size=None, text=meter_label)
        needs_boot = doboot or self.has_install_phase()

        if guest.type == "vz":
            if transient:
                raise RuntimeError(_("Domain type 'vz' doesn't support "
                    "transient installs."))
            domain = self._manual_transient_create(
                    install_xml, final_xml, needs_boot)

        else:
            if transient or needs_boot:
                domain = self.conn.createXML(install_xml or final_xml, 0)
            if not transient:
                domain = self.conn.defineXML(final_xml)

        try:
            logging.debug("XML fetched from libvirt object:\n%s",
                          domain.XMLDesc(0))
        except Exception as e:
            logging.debug("Error fetching XML from libvirt object: %s", e)
        return domain

    def _flag_autostart(self, domain):
        """
        Set the autostart flag for domain if the user requested it
        """
        try:
            domain.setAutostart(True)
        except libvirt.libvirtError as e:
            if util.is_error_nosupport(e):
                logging.warning("Could not set autostart flag: libvirt "
                             "connection does not support autostart.")
            else:
                raise e


    ######################
    # Public install API #
    ######################

    def start_install(self, guest, meter=None,
                      dry=False, return_xml=False,
                      doboot=True, transient=False):
        """
        Begin the guest install. Will add install media to the guest config,
        launch it, then redefine the XML with the postinstall config.

        :param return_xml: Don't create the guest, just return generated XML
        """
        guest.validate_name(guest.conn, guest.name)
        self.set_install_defaults(guest)

        try:
            self._prepare(guest, meter)

            if not dry:
                for dev in guest.devices.disk:
                    dev.build_storage(meter)

            install_xml, final_xml = self._build_xml(guest)
            if return_xml:
                return (install_xml, final_xml)
            if dry:
                return

            domain = self._create_guest(
                    guest, meter, install_xml, final_xml,
                    doboot, transient)

            if self.autostart:
                self._flag_autostart(domain)
            return domain
        finally:
            self._cleanup(guest)

    def get_created_disks(self, guest):
        return [d for d in guest.devices.disk if d.storage_was_created]

    def cleanup_created_disks(self, guest, meter):
        """
        Remove any disks we created as part of the install. Only ever
        called by clients.
        """
        clean_disks = self.get_created_disks(guest)
        if not clean_disks:
            return

        for disk in clean_disks:
            logging.debug("Removing created disk path=%s vol_object=%s",
                disk.path, disk.get_vol_object())
            name = os.path.basename(disk.path)

            try:
                meter.start(size=None, text=_("Removing disk '%s'") % name)

                if disk.get_vol_object():
                    disk.get_vol_object().delete()
                else:
                    os.unlink(disk.path)

                meter.end(0)
            except Exception as e:
                logging.debug("Failed to remove disk '%s'",
                    name, exc_info=True)
                logging.error("Failed to remove disk '%s': %s", name, e)
