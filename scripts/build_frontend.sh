#!/usr/bin/env bash
set -e

npm ci --prefix frontend
npm run build --prefix frontend
