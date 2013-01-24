import pytest
from . import harness
from . logger import log
from . config import DEFAULT_SHARING_CLONES
from . config import DEFAULT_COW_SLACK
from . config import DEFAULT_SHARE_RATIO

class TestSharing(harness.TestCase):
    @harness.hosttest
    def test_sharing(self, image_finder):
        # Make sure we should run
        if self.config.test_sharing_disable:
            log.info("Skipping sharing test on user request.")
            pytest.skip()

        # The user could have specified really silly or bogus knobs. Casting
        # bogosity will kill the test on purpose.
        self.config.test_sharing_sharing_clones =\
            int(self.config.test_sharing_sharing_clones)
        if self.config.test_sharing_sharing_clones < 2 or\
           self.config.test_sharing_sharing_clones > 10:
            log.info("Provided sharing clones %d will break the test, changing"
                      " to %d." % (self.config.test_sharing_sharing_clones,\
                                   DEFAULT_SHARING_CLONES))
            self.config.test_sharing_sharing_clones = DEFAULT_SHARING_CLONES
        self.config.test_sharing_cow_slack =\
            int(self.config.test_sharing_cow_slack)
        if self.config.test_sharing_cow_slack < 0 or\
           self.config.test_sharing_cow_slack > (16 * 256):
            log.info("Provided cow slack %d will break the test, changing"
                      " to %d." % (self.config.test_sharing_cow_slack,\
                                   DEFAULT_COW_SLACK))
            self.config.test_sharing_cow_slack = DEFAULT_COW_SLACK
        self.config.test_sharing_share_ratio =\
            float(self.config.test_sharing_share_ratio)
        if self.config.test_sharing_share_ratio < 0.25 or\
           self.config.test_sharing_share_ratio > 0.99:
            log.info("Provided sharing ratio %f will break the test, changing"
                      " to %d." % (self.config.test_sharing_share_ratio,
                                   DEFAULT_SHARE_RATIO))
            self.config.test_sharing_share_ratio = DEFAULT_SHARE_RATIO

        with self.harness.blessed(image_finder) as blessed:

            # Launch until we have test_sharing_sharing_clones clones on one host.
            hostdict = {}
            clonelist = []

            while True:
                clone = blessed.launch()
    
                # Surely a simpler way to do this.
                clonelist.append(clone)

                # Mark the host the holds this VM.
                host = clone.get_host()
                (hostcount, host_clone_list) = hostdict.get(host.id, (0, []))
                hostcount += 1
                host_clone_list.append(clone)
                hostdict[host.id] = (hostcount, host_clone_list)

                # If we've got enough, break.
                if hostcount == self.config.test_sharing_sharing_clones:
                    break
   
            # Figure out the generation ID.
            vmsctl = clone.vmsctl()
            generation = vmsctl.generation()
            for clone in clonelist:
                vmsctl = clone.vmsctl()
                assert generation == vmsctl.generation()
   
            # The last host bumped the sharing count.
            (hostcount, sharingclones) = hostdict[host.id]
            assert hostcount == self.config.test_sharing_sharing_clones
    
            # Set all these guys up.
            for clone in sharingclones:
                vmsctl = clone.vmsctl()
                vmsctl.pause()
                vmsctl.set_flag("share.enabled")
                vmsctl.set_flag("share.onfetch")

                # We want it to fetch and share zero pages as well. We want the
                # full hoard to complete up to the max footprint. Otherwise our
                # arithmetic below will be borked.
                vmsctl.clear_flag("zeros.enabled")
                vmsctl.clear_target()
    
            # Make them hoard.
            for clone in sharingclones:
                vmsctl = clone.vmsctl()
                assert vmsctl.full_hoard()
    
            # There should be significant sharing going on now.
            stats = host.get_vmsfs_stats(generation)
            resident = stats['cur_resident']
            allocated = stats['cur_allocated']
            expect_ratio = float(self.config.test_sharing_sharing_clones) *\
                                 self.config.test_sharing_share_ratio
            real_ratio = float(resident) / float(allocated)
            log.debug("For %d clones on host %s: resident %d allocated %d ratio %f expect %f"
                        % (self.config.test_sharing_sharing_clones, str(host), resident,
                           allocated, real_ratio, expect_ratio))
            assert real_ratio > expect_ratio
    
            # Release the brakes on the clones and assert some cow happened.
            for clone in sharingclones:
                vmsctl = clone.vmsctl()
                vmsctl.unpause()
                clone.root_command('uptime')

            stats = host.get_vmsfs_stats(generation)
            assert stats['sh_cow'] > 0
    
            # Pause everyone again to ensure no leaks happen via the sh_un stat.
            for clone in sharingclones:
                vmsctl = clone.vmsctl()
                vmsctl.pause()
    
            # Select the clone we'll be forcing CoW on.
            clone = sharingclones[0]
            vmsctl = clone.vmsctl()
    
            # Calculate file size, 256 MiB or 90% of the max.
            maxmem = vmsctl.get_max_memory()
            target = min(256 * 256, int(0.9 * float(maxmem)))
    
            # Record the CoW statistics before we begin forcing CoW.
            stats = host.get_vmsfs_stats(generation)
            unshare_before_force_cow = stats['sh_cow'] + stats['sh_un']
    
            # Force CoW on our selected clone.
            vmsctl.unpause()
    
            # Make room.
            clone.drop_caches()
    
            # The tmpfs should be allowed to fit the file plus
            # 4MiBs of headroom (inodes and blah).
            tmpfs_size = (target + (256 * 4)) * 4096
            clone.root_command("mount -o remount,size=%d /dev/shm" % (tmpfs_size))
    
            # And do it.
            clone.root_command("dd if=/dev/urandom of=/dev/shm/file bs=4k count=%d" % (target))
    
            # Figure out the impact of forcing CoW.
            stats = host.get_vmsfs_stats(generation)
            assert (stats['sh_cow'] + stats['sh_un'] - unshare_before_force_cow) >\
                   (target - self.config.test_sharing_cow_slack)
    
            # Clean up.
            for clone in clonelist:
                clone.delete()
