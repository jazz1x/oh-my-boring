---
id: eval-cache-split-brain
title: Cache cluster could not elect a leader after a network partition
kind: note
origin: personal
date: "2026-07-22"
tags:
  - caching
  - distributed-systems
  - consensus
  - split-brain
concepts:
  - leader election
  - split brain
  - quorum
  - network partition
relates_to: []
summary: A network partition left two cache nodes both claiming to be primary because neither could reach a majority; requiring a witness node and strict majority voting restored a single leader.
---

# Cache cluster could not elect a leader after a network partition

A three-node cache cluster lost connectivity between two data centers. Each side could still see its local node and one remote node, so neither side held a strict majority. Both sides promoted their local node to primary and accepted writes. When the partition healed, the cluster had two divergent datasets and no automatic way to reconcile them — a classic split-brain.

Fix: require a witness node or an odd number of voters so that only one side can ever claim a majority. A node must hold the majority lease before accepting writes. If it cannot see the majority, it steps down to follower and rejects writes rather than risking divergence. Leadership becomes a quorum property, not a local decision.
