#!/bin/bash
set -e

DEST=$(pwd)

SCRIPTDIR=$(dirname "$0")

MF2TAR=$SCRIPTDIR/mf2tar.py

URL=$(pkg publisher -HF tsv | awk 'NR==1{print $7}')
PUB=$(pkg publisher -HF tsv | awk 'NR==1{print $1}')

PERL_PKG=$(pkg contents -H -o fmri -t depend runtime/perl |head -1)
PYTHON_PKG=$(pkg contents -H -o fmri -t depend runtime/python |head -1)

[ -n "$PERL_PKG" ] || { echo "PERL_PKG?"; exit 1; }
[ -n "$PYTHON_PKG" ] || { echo "PYTHON_PKG?"; exit 1; }

PACKAGES=(
    library/glib2
    library/glib2/32
    library/libxml2
    library/security/openssl-3
    library/security/trousers
    ${PERL_PKG}
    ${PYTHON_PKG}
    print/cups
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
    etc
    lib
    var
)

if [ ! -d ${DEST}/repo ]
then
  mkdir -p ${DEST}/repo
  pkgrepo create ${DEST}/repo
  pkgrecv -s ${URL} -d ${DEST}/repo "${PACKAGES[@]}"
fi

if [ ! -f ${DEST}/adjuncts.tar ]
then
  ${MF2TAR} \
    --repository ${DEST}/repo/publisher/${PUB} \
    ${PACKAGES[@]/#/-P } \
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
