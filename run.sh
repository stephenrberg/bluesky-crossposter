#!/bin/bash

BASE_DELAY=300
while :; do
  python run.py
  VARIANCE=$(( (RANDOM % 121) - 60 ))
  SLEEP_SECS=$(( BASE_DELAY + VARIANCE ))
  sleep $SLEEP_SECS
done