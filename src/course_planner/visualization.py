from __future__ import annotations

from collections.abc import Mapping, Sequence

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import streamlit as st

from .constants import DAY_MAP, DISPLAY_END_HOUR, DISPLAY_START_HOUR, WEEKDAYS
from .schedule import ScheduledSection


def visualize_schedule(
    selected_sections: Sequence[ScheduledSection],
    course_labels: Mapping[str, str],
    title: str = "Schedule",
) -> None:
    figure, axis = plt.subplots(figsize=(10, 6))
    times = range(DISPLAY_START_HOUR, DISPLAY_END_HOUR)
    axis.set_xticks(range(len(WEEKDAYS)), WEEKDAYS)
    axis.set_yticks(range(len(times)), [f"{hour}:00" for hour in times])

    for selected in selected_sections:
        section = selected.section
        label = f"{section.code}\n{course_labels[section.code]}\n{section.instructor}"
        for meeting in section.meetings:
            day_index = DAY_MAP[meeting.day]
            y_position = meeting.start_minute / 60 - DISPLAY_START_HOUR
            height = (meeting.end_minute - meeting.start_minute) / 60
            axis.add_patch(patches.Rectangle((day_index, y_position), 0.8, height, color="orange", alpha=0.6))
            axis.text(
                day_index + 0.4,
                y_position + height / 2,
                label,
                ha="center",
                va="center",
                fontsize=8,
                wrap=True,
            )

    axis.set_xlim(-0.5, len(WEEKDAYS) - 0.5)
    axis.set_ylim(DISPLAY_END_HOUR - DISPLAY_START_HOUR, 0)
    axis.set_ylabel("Time")
    axis.set_title(title)
    axis.grid(True)
    figure.tight_layout()
    st.pyplot(figure)
    plt.close(figure)
