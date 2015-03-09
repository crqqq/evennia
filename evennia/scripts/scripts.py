"""
This module contains the base DefaultScript class that all
scripts are inheriting from.

It also defines a few common scripts.
"""

from twisted.internet.defer import Deferred, maybeDeferred
from twisted.internet.task import LoopingCall
from django.utils.translation import ugettext as _
from evennia.typeclasses.models import TypeclassBase
from evennia.scripts.models import ScriptDB
from evennia.scripts.manager import ScriptManager
from evennia.utils import logger

__all__ = ["DefaultScript", "DoNothing", "Store"]


_GA = object.__getattribute__
_SESSIONS = None

class ExtendedLoopingCall(LoopingCall):
    """
    LoopingCall that can start at a delay different
    than `self.interval`.
    """
    start_delay = None
    callcount = 0

    def start(self, interval, now=True, start_delay=None, count_start=0):
        """
        Start running function every interval seconds.

        This overloads the LoopingCall default by offering
        the start_delay keyword and ability to repeat.

        start_delay: The number of seconds before starting.
                     If None, wait interval seconds. Only
                     valid is now is False.
        repeat_start: the task will track how many times it has run.
                      this will change where it starts counting from.
                      Note that as opposed to Twisted's inbuild
                      counter, this will count also if force_repeat()
                      was called (so it will not just count the number
                      of interval seconds since start).
        """
        assert not self.running, ("Tried to start an already running "
                                  "ExtendedLoopingCall.")
        if interval < 0:
            raise ValueError, "interval must be >= 0"

        self.running = True
        d = self.deferred = Deferred()
        self.starttime = self.clock.seconds()
        self._expectNextCallAt = self.starttime
        self.interval = interval
        self._runAtStart = now
        self.callcount = max(0, count_start)

        if now:
            self()
        else:
            if start_delay is not None and start_delay >= 0:
                # we set `start_delay` after the `_reschedule` call to make
                # next_call_time() find it until next reschedule.
                self.interval = start_delay
                self._reschedule()
                self.interval = interval
                self.start_delay = start_delay
            else:
                self._reschedule()
        return d

    def __call__(self):
        "tick one step"
        self.callcount += 1
        super(ExtendedLoopingCall, self).__call__()

    def _reschedule(self):
        """
        Handle call rescheduling including
        nulling `start_delay` and stopping if
        number of repeats is reached.
        """
        self.start_delay = None
        super(ExtendedLoopingCall, self)._reschedule()

    def force_repeat(self):
        "Force-fire the callback"
        assert self.running, ("Tried to fire an ExtendedLoopingCall "
                              "that was not running.")
        if self.call is not None:
            self.call.cancel()
            self._expectNextCallAt = self.clock.seconds()
            self()

    def next_call_time(self):
        """
        Return the time in seconds until the next call. This takes
        `start_delay` into account.
        """
        if self.running:
            currentTime = self.clock.seconds()
            return self._expectNextCallAt - currentTime
        return None

#
# Base script, inherit from DefaultScript below instead.
#
class ScriptBase(ScriptDB):
    """
    Base class for scripts. Don't inherit from this, inherit
    from the class `DefaultScript` below instead.
    """
    __metaclass__ = TypeclassBase
    objects = ScriptManager()

    def __eq__(self, other):
        """
        This has to be located at this level, having it in the
        parent doesn't work.
        """
        try:
            return other.dbid == self.dbid
        except Exception:
            return False

    def _start_task(self):
        "start task runner"

        self.ndb._task = ExtendedLoopingCall(self._step_task)

        if self.db._paused_time:
            # the script was paused; restarting
            callcount = self.db._paused_callcount or 0
            self.ndb._task.start(self.db_interval,
                                 now=False,
                                 start_delay=self.db._paused_time,
                                 count_start=callcount)
            del self.db._paused_time
            del self.db._paused_repeats
        else:
            # starting script anew
            self.ndb._task.start(self.db_interval,
                                 now=not self.db_start_delay)

    def _stop_task(self):
        "stop task runner"
        task = self.ndb._task
        if task and task.running:
            task.stop()

    def _step_errback(self, e):
        "callback for runner errors"
        cname = self.__class__.__name__
        estring = _("Script %(key)s(#%(dbid)s) of type '%(cname)s': at_repeat() error '%(err)s'.") % \
                          {"key": self.key, "dbid": self.dbid, "cname": cname,
                           "err": e.getErrorMessage()}
        try:
            self.db_obj.msg(estring)
        except Exception:
            pass
        logger.log_errmsg(estring)

    def _step_callback(self):
        "step task runner. No try..except needed due to defer wrap."

        if not self.is_valid():
            self.stop()
            return

        # call hook
        self.at_repeat()

        # check repeats
        callcount = self.ndb._task.callcount
        maxcount = self.db_repeats
        if maxcount > 0 and maxcount <= callcount:
            #print "stopping script!"
            self.stop()

    def _step_task(self):
        "Step task. This groups error handling."
        try:
            return maybeDeferred(self._step_callback).addErrback(self._step_errback)
        except Exception:
            logger.log_trace()

    # Public methods

    def time_until_next_repeat(self):
        """
        Returns the time in seconds until the script will be
        run again. If this is not a stepping script, returns `None`.
        This is not used in any way by the script's stepping
        system; it's only here for the user to be able to
        check in on their scripts and when they will next be run.
        """
        task = self.ndb._task
        if task:
            try:
                return int(round(task.next_call_time()))
            except TypeError:
                pass
        return None

    def remaining_repeats(self):
        "Get the number of returning repeats. Returns `None` if unlimited repeats."
        task = self.ndb._task
        if task:
            return max(0, self.db_repeats - task.callcount)

    def start(self, force_restart=False):
        """
        Called every time the script is started (for
        persistent scripts, this is usually once every server start)

        force_restart - if True, will always restart the script, regardless
                        of if it has started before.

        returns 0 or 1 to indicated the script has been started or not.
                Used in counting.
        """

        if self.is_active and not force_restart:
            # script already runs and should not be restarted.
            return 0

        obj = self.obj
        if obj:
            # check so the scripted object is valid and initalized
            try:
                obj.cmdset
            except AttributeError:
                # this means the object is not initialized.
                logger.log_trace()
                self.is_active = False
                return 0

        # try to restart a paused script
        if self.unpause():
            return 1

        # start the script from scratch
        self.is_active = True
        try:
            self.at_start()
        except Exception:
            logger.log_trace()

        if self.db_interval > 0:
            self._start_task()
        return 1

    def stop(self, kill=False):
        """
        Called to stop the script from running.
        This also deletes the script.

        kill - don't call finishing hooks.
        """
        #print "stopping script %s" % self.key
        #import pdb
        #pdb.set_trace()
        if not kill:
            try:
                self.at_stop()
            except Exception:
                logger.log_trace()
        self._stop_task()
        try:
            self.delete()
        except AssertionError:
            logger.log_trace()
            return 0
        return 1

    def pause(self):
        """
        This stops a running script and stores its active state.
        It WILL NOT call the `at_stop()` hook.
        """
        if not self.db._paused_time:
            # only allow pause if not already paused
            task = self.ndb._task
            if task:
                self.db._paused_time = task.next_call_time()
                self.db._paused_callcount = task.callcount
                self._stop_task()
            self.is_active = False

    def unpause(self):
        """
        Restart a paused script. This WILL call the `at_start()` hook.
        """
        if self.db._paused_time:
            # only unpause if previously paused
            self.is_active = True

            try:
                self.at_start()
            except Exception:
                logger.log_trace()

            self._start_task()
            return True

    def force_repeat(self):
        """
        Fire a premature triggering of the script callback. This
        will reset the timer and count down repeats as if the script
        had fired normally.
        """
        task = self.ndb._task
        if task:
            task.force_repeat()

    # hooks
    def at_script_creation(self):
        "placeholder"
        pass

    def is_valid(self):
        "placeholder"
        pass

    def at_start(self):
        "placeholder."
        pass

    def at_stop(self):
        "placeholder"
        pass

    def at_repeat(self):
        "placeholder"
        pass

    def at_init(self):
        "called when typeclass re-caches. Usually not used for scripts."
        pass

#
# Base Script - inherit from this
#

class DefaultScript(ScriptBase):
    """
    This is the base TypeClass for all Scripts. Scripts describe
    events, timers and states in game, they can have a time component
    or describe a state that changes under certain conditions.

    Script API:

    * Available properties (only available on initiated Typeclass objects)

     key (string) - name of object
     name (string)- same as key
     aliases (list of strings) - aliases to the object. Will be saved to
     database as AliasDB entries but returned as strings.
     dbref (int, read-only) - unique #id-number. Also "id" can be used.
     date_created (string) - time stamp of object creation
     permissions (list of strings) - list of permission strings

     desc (string)      - optional description of script, shown in listings
     obj (Object)       - optional object that this script is connected to
                          and acts on (set automatically
                          by `obj.scripts.add()`)
     interval (int)     - how often script should run, in seconds.
                          <=0 turns off ticker
     start_delay (bool) - if the script should start repeating right
                          away or wait self.interval seconds
     repeats (int)      - how many times the script should repeat before
                          stopping. <=0 means infinite repeats
     persistent (bool)  - if script should survive a server shutdown or not
     is_active (bool)   - if script is currently running

    * Handlers

     locks - lock-handler: use locks.add() to add new lock strings
     db - attribute-handler: store/retrieve database attributes on this
          self.db.myattr=val, val=self.db.myattr
     ndb - non-persistent attribute handler: same as db but does not
           create a database entry when storing data

    * Helper methods

     start() - start script (this usually happens automatically at creation
               and obj.script.add() etc)
     stop()  - stop script, and delete it
     pause() - put the script on hold, until unpause() is called. If script
               is persistent, the pause state will survive a shutdown.
     unpause() - restart a previously paused script. The script will
                 continue as if it was never paused.
     force_repeat() - force-step the script, regardless of how much remains
                until next step. This counts like a normal firing in all ways.
     time_until_next_repeat() - if a timed script (interval>0), returns
                time until next tick
     remaining_repeats() - number of repeats remaining, if limited

    * Hook methods

     at_script_creation() - called only once, when an object of this
                            class is first created.
     is_valid() - is called to check if the script is valid to be running
                  at the current time. If is_valid() returns False, the
                  running script is stopped and removed from the game. You
                  can use this to check state changes (i.e. an script
                  tracking some combat stats at regular intervals is only
                  valid to run while there is actual combat going on).
      at_start() - Called every time the script is started, which for
                  persistent scripts is at least once every server start.
                  Note that this is unaffected by self.delay_start, which
                  only delays the first call to at_repeat(). It will also
                  be called after a pause, to allow for setting up the script.
      at_repeat() - Called every self.interval seconds. It will be called
                  immediately upon launch unless self.delay_start is True,
                  which will delay the first call of this method by
                  self.interval seconds. If self.interval<=0, this method
                  will never be called.
      at_stop() - Called as the script object is stopped and is about to
                  be removed from the game, e.g. because is_valid()
                  returned False or self.stop() was called manually.
      at_server_reload() - Called when server reloads. Can be used to save
                  temporary variables you want should survive a reload.
      at_server_shutdown() - called at a full server shutdown.

    """
    def at_first_save(self):
        """
        This is called after very first time this object is saved.
        Generally, you don't need to overload this, but only the hooks
        called by this method.
        """
        self.at_script_creation()

        if hasattr(self, "_createdict"):
            # this will only be set if the utils.create_script
            # function was used to create the object. We want
            # the create call's kwargs to override the values
            # set by hooks.
            cdict = self._createdict
            updates = []
            if not cdict.get("key"):
                if not self.db_key:
                    self.db_key = "#%i" % self.dbid
                    updates.append("db_key")
            elif self.db_key != cdict["key"]:
                self.db_key = cdict["key"]
                updates.append("db_key")
            if cdict.get("interval") and self.interval != cdict["interval"]:
                self.db_interval = cdict["interval"]
                updates.append("db_interval")
            if cdict.get("start_delay") and self.start_delay != cdict["start_delay"]:
                self.db_start_delay = cdict["start_delay"]
                updates.append("db_start_delay")
            if cdict.get("repeats") and self.repeats != cdict["repeats"]:
                self.db_repeats = cdict["repeats"]
                updates.append("db_repeats")
            if cdict.get("persistent") and self.persistent != cdict["persistent"]:
                self.db_persistent = cdict["persistent"]
                updates.append("db_persistent")
            if updates:
                self.save(update_fields=updates)
            if not cdict.get("autostart"):
                # don't auto-start the script
                return

        # auto-start script (default)
        self.start()


    def at_script_creation(self):
        """
        Only called once, by the create function.
        """
        pass

    def is_valid(self):
        """
        Is called to check if the script is valid to run at this time.
        Should return a boolean. The method is assumed to collect all needed
        information from its related self.obj.
        """
        return not self._is_deleted

    def at_start(self):
        """
        Called whenever the script is started, which for persistent
        scripts is at least once every server start. It will also be called
        when starting again after a pause (such as after a server reload)
        """
        pass

    def at_repeat(self):
        """
        Called repeatedly if this Script is set to repeat
        regularly.
        """
        pass

    def at_stop(self):
        """
        Called whenever when it's time for this script to stop
        (either because is_valid returned False or it runs out of iterations)
        """
        pass

    def at_server_reload(self):
        """
        This hook is called whenever the server is shutting down for
        restart/reboot. If you want to, for example, save non-persistent
        properties across a restart, this is the place to do it.
        """
        pass

    def at_server_shutdown(self):
        """
        This hook is called whenever the server is shutting down fully
        (i.e. not for a restart).
        """
        pass


# Some useful default Script types used by Evennia.

class DoNothing(DefaultScript):
    "An script that does nothing. Used as default fallback."
    def at_script_creation(self):
        "Setup the script"
        self.key = "sys_do_nothing"
        self.desc = _("This is an empty placeholder script.")


class Store(DefaultScript):
    "Simple storage script"
    def at_script_creation(self):
        "Setup the script"
        self.key = "sys_storage"
        self.desc = _("This is a generic storage container.")
