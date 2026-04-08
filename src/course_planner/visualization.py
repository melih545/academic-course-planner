from __future__ import annotations

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from .constants import DAY_MAP, DISPLAY_END_HOUR, DISPLAY_START_HOUR, WEEKDAYS
from .preprocessing import time_to_float


def visualize_schedule(
    data: pd.DataFrame, selected_courses: list[list], title: str = "Schedule"
) -> None:
    plot_data = data.copy()
    plot_data["DayIndex"] = plot_data["Days"].map(DAY_MAP)

    figure, axis = plt.subplots(figsize=(10, 6))
    times = range(DISPLAY_START_HOUR, DISPLAY_END_HOUR)

    axis.set_xticks(range(len(WEEKDAYS)))
    axis.set_xticklabels(WEEKDAYS)
    axis.set_yticks(range(len(times)))
    axis.set_yticklabels([f"{hour}:00" for hour in times])

    for _, row in plot_data.iterrows():
        if not any(
            row["Code"] == course[0]
            and row["Days"] == course[3]
            and row["StartTimeFloat"] == course[4]
            for course in selected_courses
        ):
            continue

        try:
            if pd.isna(row["Times"]) or "/" not in row["Times"] or "490" in row["Code"]:
                continue

            start_time, end_time = row["Times"].split("/")
            y_position = time_to_float(start_time) - DISPLAY_START_HOUR
            height = time_to_float(end_time) - time_to_float(start_time)
            if height <= 0:
                continue

            rectangle = patches.Rectangle(
                (row["DayIndex"], y_position),
                0.8,
                height,
                color="orange",
                alpha=0.6,
            )
            axis.add_patch(rectangle)
            axis.text(
                row["DayIndex"] + 0.4,
                y_position + height / 2,
                f"{row['Code']}\n{row['CourseName']}\n{row['Instructors']}",
                ha="center",
                va="center",
                fontsize=8,
                wrap=True,
            )
        except Exception as error:  # pragma: no cover - Streamlit warning path.
            st.warning(f"Skipping invalid row: {row['Code']} (Error: {error})")

    axis.set_xlim(-0.5, len(WEEKDAYS) - 0.5)
    axis.set_ylim(0, DISPLAY_END_HOUR - DISPLAY_START_HOUR)
    axis.set_xlabel("Days")
    axis.set_ylabel("Time")
    axis.set_title(title)
    axis.grid(True)
    plt.tight_layout()
    st.pyplot(figure)
