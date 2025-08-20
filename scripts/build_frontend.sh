#!/usr/bin/env bash
set -e

npm install --prefix frontend
npm run build --prefix frontend
