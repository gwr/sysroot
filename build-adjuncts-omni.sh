#!/bin/bash
set -e

DEST=$(pwd)

SCRIPTDIR=$(dirname "$0")

MF2TAR=$SCRIPTDIR/mf2tar.py
FIX_PUB_MOG=$SCRIPTDIR/fix-omni-pub.mog

URL=$(pkg publisher -HF tsv | awk '$1=="omnios"{print $7}')
EXTRA_URL=$(pkg publisher -HF tsv | awk '$1=="extra.omnios"{print $7}')
PUB="omnios"

PERL_PKG="runtime/perl"
PYTHON_PKG="runtime/python-313"

# cups comes from the extra.omnios publisher; it is re-published into the
# local repo under the omnios publisher name using a MOG transform.
PKG_CUPS="ooce/print/cups"

[ -n "$PERL_PKG" ] || { echo "PERL_PKG?"; exit 1; }
[ -n "$PYTHON_PKG" ] || { echo "PYTHON_PKG?"; exit 1; }

PACKAGES=(
    library/glib2
    library/libxml2
    library/security/openssl-3
    library/security/trousers
    ${PERL_PKG}
    ${PYTHON_PKG}
    system/library/libdbus
    system/library/mozilla-nss
    system/management/snmp/net-snmp
)

EXCLUDES=(
    usr/lib/cups
    'usr/lib/python*'
    'usr/openssl/*/share'
    usr/perl5
    usr/share
    usr/bin
    usr/sbin
    usr/sfw
    usr/ssl-3/man
    opt/ooce/cups
    opt/ooce/share
    etc
    lib
    var
)

if [ ! -d ${DEST}/repo ]
then
  mkdir -p ${DEST}/repo
  pkgrepo create ${DEST}/repo
  pkgrecv -s ${URL} -d ${DEST}/repo "${PACKAGES[@]}"
  pkgrecv -s ${EXTRA_URL} -d ${DEST}/repo --mog-file ${FIX_PUB_MOG} ${PKG_CUPS}
fi

if [ ! -f ${DEST}/adjuncts.tar ]
then
  ${MF2TAR} \
    --repository ${DEST}/repo/publisher/${PUB} \
    ${PACKAGES[@]/#/-P } -P ${PKG_CUPS} \
    ${EXCLUDES[@]/#/-E } \
    ${DEST}/adjuncts.tar
fi

tar tf ${DEST}/adjuncts.tar |sort > adjuncts.sort

if [ -d "${DEST}/proto" ]
then
  echo "Already have ./proto; skip untar!"
else
  mkdir -p ${DEST}/proto
  tar xf ${DEST}/adjuncts.tar -C ${DEST}/proto
fi
