# Copyright (C) 2013 Red Hat, Inc.
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import logging
import os
import re
import sys
import time
import traceback
import unittest

from tests import utils

from virtinst import Guest
from virtinst import urldetect
from virtinst import urlfetcher
from virtinst import util
from virtinst.urldetect import ALTLinuxDistro
from virtinst.urldetect import CentOSDistro
from virtinst.urldetect import DebianDistro
from virtinst.urldetect import FedoraDistro
from virtinst.urldetect import GenericTreeinfoDistro
from virtinst.urldetect import MandrivaDistro
from virtinst.urldetect import RHELDistro
from virtinst.urldetect import SuseDistro
from virtinst.urldetect import UbuntuDistro


class _URLTestData(object):
    """
    Class that tracks all data needed for a single URL test case.
    Data is stored in test_urls.ini
    """
    def __init__(self, name, url, detectdistro,
            testxen, testshortcircuit, kernelarg, kernelregex):
        self.name = name
        self.url = url
        self.detectdistro = detectdistro
        self.arch = self._find_arch()
        self.distroclass = self._distroclass_for_name(self.name)
        self.kernelarg = kernelarg
        self.kernelregex = kernelregex

        self.testxen = testxen

        # If True, pass in the expected distro value to getDistroStore
        # so it can short circuit the lookup checks. Speeds up the tests
        # and exercises the shortcircuit infrastructure
        self.testshortcircuit = testshortcircuit

    def _distroclass_for_name(self, name):
        # Map the test case name to the expected urldetect distro
        # class we should be detecting
        if "fedora" in name:
            return FedoraDistro
        if "centos" in name:
            return CentOSDistro
        if "rhel" in name:
            return RHELDistro
        if "suse" in name:
            return SuseDistro
        if "debian" in name:
            return DebianDistro
        if "ubuntu" in name:
            return UbuntuDistro
        if "mageia" in name:
            return MandrivaDistro
        if "altlinux" in name:
            return ALTLinuxDistro
        if "generic" in name:
            return GenericTreeinfoDistro
        raise RuntimeError("name=%s didn't map to any distro class. Extend "
            "_distroclass_for_name" % name)

    def _find_arch(self):
        if ("i686" in self.url or
            "i386" in self.url or
            "i586" in self.url):
            return "i686"
        if ("arm64" in self.url or
            "aarch64" in self.url):
            return "aarch64"
        if ("ppc64el" in self.url or
            "ppc64le" in self.url):
            return "ppc64le"
        if "s390" in self.url:
            return "s390x"
        if ("x86_64" in self.url or
            "amd64" in self.url):
            return "x86_64"
        return "x86_64"

testconn = utils.URIs.open_testdefault_cached()
hvmguest = Guest(testconn)
hvmguest.os.os_type = "hvm"
xenguest = Guest(testconn)
xenguest.os.os_type = "xen"

meter = util.make_meter(quiet=not utils.clistate.debug)


def _storeForDistro(fetcher, guest):
    """
    Helper to lookup the Distro store object, basically detecting the
    URL. Handle occasional proxy errors
    """
    for ignore in range(0, 10):
        try:
            return urldetect.getDistroStore(guest, fetcher)
        except Exception as e:
            if "502" in str(e):
                logging.debug("Caught proxy error: %s", str(e))
                time.sleep(.5)
                continue
            raise
    raise  # pylint: disable=misplaced-bare-raise


def _sanitize_osdict_name(detectdistro):
    if detectdistro in ["none", "None", None]:
        return None
    return detectdistro


def _testURL(fetcher, testdata):
    """
    Test that our URL detection logic works for grabbing kernels
    """
    distname = testdata.name
    arch = testdata.arch
    detectdistro = _sanitize_osdict_name(testdata.detectdistro)

    hvmguest.os.arch = arch
    xenguest.os.arch = arch
    if testdata.testshortcircuit:
        hvmguest.set_os_name(detectdistro)
        xenguest.set_os_name(detectdistro)

    try:
        hvmstore = _storeForDistro(fetcher, hvmguest)
        xenstore = None
        if testdata.testxen:
            xenstore = _storeForDistro(fetcher, xenguest)
    except Exception:
        raise AssertionError("\nFailed to detect URLDistro class:\n"
            "name   = %s\n"
            "url    = %s\n\n%s" %
            (distname, fetcher.location, "".join(traceback.format_exc())))

    for s in [hvmstore, xenstore]:
        if not s:
            continue

        if not isinstance(s, testdata.distroclass):
            raise AssertionError("Unexpected URLDistro class:\n"
                "found  = %s\n"
                "expect = %s\n\n"
                "testname = %s\n"
                "url      = %s" %
                (s.__class__, testdata.distroclass, distname,
                 fetcher.location))

        # Make sure the stores are reporting correct distro name/variant
        if detectdistro != s.get_osdict_info():
            raise AssertionError(
                "Detected OS did not match expected values:\n"
                "found   = %s\n"
                "expect  = %s\n\n"
                "testname = %s\n"
                "url      = %s\n"
                "store    = %s" %
                (s.get_osdict_info(), detectdistro,
                 distname, fetcher.location, testdata.distroclass))

    # Fetch regular kernel
    kernel, initrd = hvmstore.check_kernel_paths()
    dummy = initrd
    if testdata.kernelregex and not re.match(testdata.kernelregex, kernel):
        raise AssertionError("kernel=%s but testdata.kernelregex='%s'" %
                (kernel, testdata.kernelregex))

    kernelargs = hvmstore.get_kernel_url_arg()
    if testdata.kernelarg == "None":
        if bool(kernelargs):
            raise AssertionError("kernelargs='%s' but testdata.kernelarg='%s'"
                    % (kernelargs, testdata.kernelarg))
    elif testdata.kernelarg:
        if not kernelargs == testdata.kernelarg:
            raise AssertionError("kernelargs='%s' but testdata.kernelarg='%s'"
                    % (kernelargs, testdata.kernelarg))

    # Fetch xen kernel
    if xenstore:
        xenstore.check_kernel_paths()


def _fetchWrapper(url, cb):
    fetcher = urlfetcher.fetcherForURI(url, "/tmp", meter)
    return cb(fetcher)


def _testURLWrapper(testdata):
    os.environ.pop("VIRTINST_TEST_SUITE", None)

    logging.debug("Testing for media arch=%s distroclass=%s",
                  testdata.arch, testdata.distroclass)

    sys.stdout.write("\nTesting %-25s " % testdata.name)
    sys.stdout.flush()

    def cb(fetcher):
        return _testURL(fetcher, testdata)
    return _fetchWrapper(testdata.url, cb)


# Register tests to be picked up by unittest
class URLTests(unittest.TestCase):
    def test001BadURL(self):
        badurl = "http://aksdkakskdfa-idontexist.com/foo/tree"
        def cb(fetcher):
            return _storeForDistro(fetcher, hvmguest)

        try:
            _fetchWrapper(badurl, cb)
            raise AssertionError("Expected URL failure")
        except ValueError as e:
            self.assertTrue("maybe you mistyped" in str(e))



def _make_tests():
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read("tests/test_urls.ini")

    manualpath = "~/.config/virt-manager/test_urls_manual.ini"
    cfg.read(os.path.expanduser(manualpath))
    if not os.path.exists(os.path.expanduser(manualpath)):
        print("NOTE: Pass in manual data with %s" % manualpath)

    urls = {}
    for name in cfg.sections():
        vals = dict(cfg.items(name))
        d = _URLTestData(name, vals["url"],
                vals.get("distro", None),
                vals.get("testxen", "0") == "1",
                vals.get("testshortcircuit", "0") == "1",
                vals.get("kernelarg", None),
                vals.get("kernelregex", None))
        urls[d.name] = d

    for key, testdata in sorted(urls.items()):
        def _make_wrapper(d):
            return lambda _self: _testURLWrapper(d)
        setattr(URLTests, "testURL%s" % key.replace("-", "_"),
                _make_wrapper(testdata))

_make_tests()
