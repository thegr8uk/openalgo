#!/usr/bin/env bash

DOCKER_USERNAME="${DOCKER_USERNAME:-}"

if [ -z "$DOCKER_USERNAME" ]; then
	echo "Error: DOCKER_USERNAME environment variable is not set." >&2
	echo "Please set DOCKER_USERNAME to your Docker Hub username and retry." >&2
	exit 1
fi

IMAGE_NAME="${DOCKER_USERNAME}/openalgo:latest"

docker build -t "$IMAGE_NAME" .
docker push "$IMAGE_NAME"