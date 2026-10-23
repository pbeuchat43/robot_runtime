"""Metadata separation shared by the private watch and browser delivery."""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul N. Beuchat

TOPIC_METADATA = ("id", "label", "unit_id", "node", "name", "type", "max_rate_hz",
                  "stale_after_seconds", "fields", "projection", "source_clock", "receipt_clock")
AGES = ("age_seconds", "acquisition_age_seconds", "sample_age_seconds", "progress_age_seconds")


def split_topic(topic):
    return ({key: topic[key] for key in TOPIC_METADATA if key in topic},
            {key: value for key, value in topic.items() if key not in TOPIC_METADATA or key == "id"})
