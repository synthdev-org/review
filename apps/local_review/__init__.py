"""apps.local_review — developer-only composition root: run the review
pipeline on a local `base...head` git range and print findings. Not deployed
(absent from the Dockerfile/Helm chart); a CLI over the same steps the Worker
runs, with the GitHub and Postgres adapters swapped for local ones."""
