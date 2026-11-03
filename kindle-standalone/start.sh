#!/bin/sh
BASE=/mnt/us/kindle-standalone
PYTHON=/mnt/us/python3/bin/python3.9
LOCK=/tmp/kindle-standalone.lock
[ "$(id -u)" = 0 ] || exit 1
sleep 3
printf '\nSTANDALONE START %s\n' "$(date)"
if ! mkdir "$LOCK" 2>/dev/null; then
    OLD_PID=$(cat "$LOCK/pid" 2>/dev/null)
    case "$OLD_PID" in
        ''|*[!0-9]*) ;;
        *)
            if [ -r "/proc/$OLD_PID/cmdline" ]; then
                case "$(tr '\000' ' ' < "/proc/$OLD_PID/cmdline")" in
                    *"sh /mnt/us/kindle-standalone/start.sh"*)
                        if [ -f "$BASE/data/restart-required" ]; then
                            kill -TERM "$OLD_PID"
                            WAITED=0
                            while kill -0 "$OLD_PID" 2>/dev/null && [ "$WAITED" -lt 30 ]; do
                                sleep 1
                                WAITED=$((WAITED+1))
                            done
                            if kill -0 "$OLD_PID" 2>/dev/null; then
                                echo 'Previous dashboard is still closing; open again shortly.'
                                exit 4
                            fi
                        else
                            touch /tmp/kindle-standalone-show
                            echo ALREADY_RUNNING
                            exit 0
                        fi
                        ;;
                esac
            fi
            ;;
    esac
    rm -f "$LOCK/pid"
    if [ -d "$LOCK" ]; then rmdir "$LOCK" 2>/dev/null || exit 2; fi
    mkdir "$LOCK" || exit 3
fi
echo $$ > "$LOCK/pid"
LAUNCHED=0
cleanup() {
    RESULT=$?
    trap - EXIT INT TERM HUP
    printf 'STANDALONE EXIT %s code=%s\n' "$(date)" "$RESULT"
    if [ "$LAUNCHED" = 1 ]; then
        lipc-set-prop com.lab126.powerd preventScreenSaver 0
        initctl start lab126_gui 2>/dev/null
    fi
    rm -f "$LOCK/pid" /tmp/kindle-standalone-show
    rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT
CHILD=
stop_child() {
    [ -z "$CHILD" ] || kill -TERM "$CHILD" 2>/dev/null
    [ -z "$CHILD" ] || wait "$CHILD"
    exit 0
}
trap stop_child INT TERM HUP
LEGACY_PID=$(cat /tmp/kindle-dashboard.lock/pid 2>/dev/null)
case "$LEGACY_PID" in
    ''|*[!0-9]*) ;;
    *)
        if [ -r "/proc/$LEGACY_PID/cmdline" ]; then
            case "$(tr '\000' ' ' < "/proc/$LEGACY_PID/cmdline")" in
                *"/mnt/us/kindle-dashboard/client"*)
                    echo 'Old dashboard is still running; exit it first.'
                    exit 4
                    ;;
            esac
        fi
        ;;
esac
export PYTHONHOME=/mnt/us/python3
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export TZ=CST-8
unset PYTHONPATH
cd "$BASE" || exit 5
rm -f "$BASE/data/restart-required"
LAUNCHED=1
"$PYTHON" -B -u "$BASE/updater.py" >> "$BASE/data/app.log" 2>&1 &
CHILD=$!
wait "$CHILD"
RESULT=$?
exit "$RESULT"
