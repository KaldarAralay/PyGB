from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Protocol


SCREEN_WIDTH = 160
SCREEN_HEIGHT = 144
DOTS_PER_LINE = 456
LINES_PER_FRAME = 154
VISIBLE_LINES = 144
MODE2_DOTS = 80
MODE3_DOTS = 172
MODE3_MAX_DOTS = 289
MODE0_DOTS = DOTS_PER_LINE - MODE2_DOTS - MODE3_DOTS
MAX_SPRITES_PER_LINE = 10
RASTER_REGISTER_WRITE_PIXEL_OFFSET = -3
BGP_REGISTER_OFFSET = 0x47

LCDC_ENABLE = 0x80
LCDC_WINDOW_TILEMAP = 0x40
LCDC_WINDOW_ENABLE = 0x20
LCDC_BG_WINDOW_TILE_DATA = 0x10
LCDC_BG_TILEMAP = 0x08
LCDC_OBJ_SIZE = 0x04
LCDC_OBJ_ENABLE = 0x02
LCDC_BG_WINDOW_ENABLE = 0x01
LCDC_FETCH_SOURCE_BITS = LCDC_WINDOW_TILEMAP | LCDC_BG_WINDOW_TILE_DATA | LCDC_BG_TILEMAP
LCDC_TILE_DATA_WRITE_FETCH_OFFSET = 3
LCDC_TILEMAP_WRITE_FETCH_OFFSET = 5
LCDC_WINDOW_TILE_DATA_LOW_WRITE_FETCH_OFFSET = 4
LCDC_WINDOW_TILE_DATA_HIGH_WRITE_FETCH_OFFSET = 2
SCX_LOW_BITS_LATCH_DOT = MODE2_DOTS + 8

OBJ_PRIORITY = 0x80
OBJ_Y_FLIP = 0x40
OBJ_X_FLIP = 0x20
OBJ_DMG_PALETTE = 0x10

MODE_HBLANK = 0
MODE_VBLANK = 1
MODE_OAM = 2
MODE_DRAWING = 3

DMG_GRAYSCALE = (
    (255, 255, 255),
    (170, 170, 170),
    (85, 85, 85),
    (0, 0, 0),
)


@lru_cache(maxsize=4096)
def _decoded_tile_row(
    lo: int,
    hi: int,
    palette: int,
) -> tuple[tuple[int, int, int, int, int, int, int, int], tuple[int, int, int, int, int, int, int, int]]:
    shades = (
        palette & 0x03,
        (palette >> 2) & 0x03,
        (palette >> 4) & 0x03,
        (palette >> 6) & 0x03,
    )
    color_ids = (
        ((hi >> 7) & 1) << 1 | ((lo >> 7) & 1),
        ((hi >> 6) & 1) << 1 | ((lo >> 6) & 1),
        ((hi >> 5) & 1) << 1 | ((lo >> 5) & 1),
        ((hi >> 4) & 1) << 1 | ((lo >> 4) & 1),
        ((hi >> 3) & 1) << 1 | ((lo >> 3) & 1),
        ((hi >> 2) & 1) << 1 | ((lo >> 2) & 1),
        ((hi >> 1) & 1) << 1 | ((lo >> 1) & 1),
        (hi & 1) << 1 | (lo & 1),
    )
    return color_ids, tuple(shades[color_id] for color_id in color_ids)


@dataclass(frozen=True)
class PPURenderState:
    lcdc: int
    scy: int
    scx: int
    bgp: int
    obp0: int
    obp1: int
    wx: int
    window_line: int
    window_y_triggered: bool


class PPUBus(Protocol):
    vram: bytearray
    oam: bytearray
    io: bytearray

    @property
    def oam_dma_active(self) -> bool:
        ...

    @property
    def interrupt_flags(self) -> int:
        ...

    @interrupt_flags.setter
    def interrupt_flags(self, value: int) -> None:
        ...


class PPU:
    def __init__(self, bus: PPUBus) -> None:
        self.bus = bus
        self.framebuffer = [[0 for _ in range(SCREEN_WIDTH)] for _ in range(SCREEN_HEIGHT)]
        self.line_dots = 0
        self._line_mode3_dots = MODE3_DOTS
        self._line_render_state: PPURenderState | None = None
        self._line_render_segments: list[tuple[int, PPURenderState]] | None = None
        self._line_row: list[int] | None = None
        self._line_bg_color_ids: list[int] | None = None
        self._line_bg_tile_data_sources: dict[int, tuple[int | None, int | None]] | None = None
        self._line_bg_tile_map_scy: dict[int, int] | None = None
        self._line_bg_tile_data_scy: dict[int, tuple[int | None, int | None]] | None = None
        self._line_lcdc_write_serial = 0
        self._line_lcdc_bg_enable_write_count = 0
        self._line_lcdc_window_enable_write_count = 0
        self._line_lcdc_write_old_value: int | None = None
        self._line_window_tile_data_source_claims: dict[tuple[int, int], int] = {}
        self._line_lcdc_tile_data_source_claims: dict[tuple[int, int], int] = {}
        self._line_pre_scroll_write_values: tuple[int, int] | None = None
        self._line_render_x = 0
        self._line_render_complete = False
        self._line_palette_phase_offset = 0
        self._line_mode3_start_scroll_write_offset = 0
        self._line_window_used = False
        self._line_window_active_at_render_x = False
        self._line_window_activation_count = 0
        self._line_window_start_glitch_x: int | None = None
        self._line_window_reactivation_glitch_x: int | None = None
        self._line_window_enable_early_pulse = False
        self._line_window_enable_cancel_glitch_x: int | None = None
        self._line_window_penalty_dots = 0
        self._line_obj_penalty_dots = 0
        self._line_forced_obj_penalty_events: list[tuple[int, int]] = []
        self._line_selected_sprites: list[tuple[int, int, int, int, int, int]] | None = None
        self._line_sprite_selection_lcdc: int | None = None
        self._line_oam_dma_seen = False
        self._line_oam_dma_hidden_x: int | None = None
        self._hblank_stat_interrupt_dot: int | None = None
        self.frame_count = 0
        self._scanline = 0
        self.window_line = 0
        self._window_y_triggered = False
        self._stat_line = False
        self._set_ly(0)
        self._set_mode(MODE_OAM if self.lcd_enabled else MODE_HBLANK, request_interrupt=False)
        self._update_lyc_flag(request_interrupt=False)
        self._check_window_y_trigger()

    @property
    def lcd_enabled(self) -> bool:
        return bool(self.bus.io[0x40] & LCDC_ENABLE)

    @property
    def mode(self) -> int:
        return self.bus.io[0x41] & 0x03

    def tick(self, cycles: int) -> None:
        if not self.lcd_enabled:
            self.line_dots = 0
            self._clear_line_rendering()
            self._scanline = 0
            self._set_ly(0, update_lyc=False)
            self._set_mode(MODE_HBLANK)
            return

        while cycles > 0:
            next_dot = DOTS_PER_LINE
            ly = self._scanline
            if ly < VISIBLE_LINES:
                if self.line_dots < MODE2_DOTS:
                    next_dot = min(next_dot, MODE2_DOTS)
                if self.mode == MODE_DRAWING:
                    next_dot = min(next_dot, MODE2_DOTS + self._line_mode3_dots)
                if self.line_dots < DOTS_PER_LINE - 4:
                    next_dot = min(next_dot, DOTS_PER_LINE - 4)
            elif ly == LINES_PER_FRAME - 1 and self.line_dots < 4:
                next_dot = min(next_dot, 4)

            if self._hblank_stat_interrupt_dot is not None:
                if self.line_dots >= self._hblank_stat_interrupt_dot:
                    self._maybe_request_pending_hblank_stat_interrupt()
                else:
                    next_dot = min(next_dot, self._hblank_stat_interrupt_dot)

            elapsed = min(cycles, max(1, next_dot - self.line_dots))
            self.line_dots += elapsed
            cycles -= elapsed

            ly = self._scanline
            if ly < VISIBLE_LINES:
                if self.line_dots == MODE2_DOTS:
                    self._start_mode3_line()
                    self._line_mode3_dots = self._mode3_duration_for_line(self._line_render_state, ly)
                    self._set_mode(MODE_DRAWING)
                elif (
                    self.mode == MODE_DRAWING
                    and self.line_dots >= MODE2_DOTS + self._line_mode3_dots
                ):
                    self._finish_mode3_line(ly)
                    self._set_mode(MODE_HBLANK, request_interrupt=False)
                    self._schedule_hblank_stat_interrupt()
                if self.line_dots == DOTS_PER_LINE - 4:
                    self._preload_next_ly(ly + 1)
            elif ly == LINES_PER_FRAME - 1 and self.line_dots == 4:
                self._set_ly(0)
            self._maybe_request_pending_hblank_stat_interrupt()

            if self.line_dots >= DOTS_PER_LINE:
                self.line_dots = 0
                self._advance_line()

    def cycles_until_next_event(self) -> int:
        if not self.lcd_enabled:
            return DOTS_PER_LINE

        next_dot = DOTS_PER_LINE
        ly = self._scanline
        if ly < VISIBLE_LINES:
            if self.line_dots < MODE2_DOTS:
                next_dot = min(next_dot, MODE2_DOTS)
            if self.mode == MODE_DRAWING:
                next_dot = min(next_dot, MODE2_DOTS + self._line_mode3_dots)
            if self.line_dots < DOTS_PER_LINE - 4:
                next_dot = min(next_dot, DOTS_PER_LINE - 4)
        elif ly == LINES_PER_FRAME - 1 and self.line_dots < 4:
            next_dot = min(next_dot, 4)

        if self._hblank_stat_interrupt_dot is not None:
            if self.line_dots >= self._hblank_stat_interrupt_dot:
                return 1
            next_dot = min(next_dot, self._hblank_stat_interrupt_dot)

        return max(1, next_dot - self.line_dots)

    def on_oam_dma_active_cycle(self) -> None:
        if (
            self.lcd_enabled
            and self._scanline < VISIBLE_LINES
            and self.mode in {MODE_OAM, MODE_DRAWING}
        ):
            if self._line_oam_dma_hidden_x is not None:
                self._line_oam_dma_seen = True
                return
            if self.mode == MODE_DRAWING and self._line_render_state is not None:
                visible_x = self._visible_pixels_emitted()
                self._render_active_line_until(visible_x)
                hidden_state = replace(
                    self._state_at_render_x(min(visible_x, SCREEN_WIDTH - 1)),
                    lcdc=self._line_render_state.lcdc & ~LCDC_OBJ_ENABLE,
                )
                self._line_oam_dma_hidden_x = visible_x
                self._apply_render_state_fields_from(visible_x, hidden_state, ("lcdc",))
                self._line_oam_dma_seen = True
                self._sync_segmented_obj_penalty()
                return
            self._line_oam_dma_hidden_x = 0
            self._line_oam_dma_seen = True

    def on_lcdc_write(self, old_value: int, new_value: int) -> None:
        was_enabled = bool(old_value & LCDC_ENABLE)
        now_enabled = bool(new_value & LCDC_ENABLE)
        if was_enabled and not now_enabled:
            self.line_dots = 0
            self._clear_line_rendering()
            self._scanline = 0
            self.window_line = 0
            self._window_y_triggered = False
            self._clear_framebuffer()
            self._set_ly(0, update_lyc=False)
            self._set_mode(MODE_HBLANK)
        elif not was_enabled and now_enabled:
            self.line_dots = 0
            self._clear_line_rendering()
            self._scanline = 0
            self.window_line = 0
            self._window_y_triggered = False
            self._set_ly(0)
            self._set_mode(MODE_HBLANK)
            self._check_window_y_trigger()

    def on_stat_write(self, value: int) -> int:
        stat = self.bus.io[0x41]
        self._maybe_request_spurious_stat_interrupt()
        return 0x80 | (value & 0x78) | (stat & 0x07)

    def on_stat_written(self) -> None:
        self._update_stat_interrupt_line()

    def on_lyc_write(self) -> None:
        if not self.lcd_enabled:
            return
        self._update_lyc_flag()

    def before_render_register_write(self, register_offset: int | None = None) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        self._render_active_line_until(self._palette_register_write_x())

    def before_lcdc_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        self._line_lcdc_write_old_value = self.bus.io[0x40]
        self._render_active_line_until(self._raster_register_write_x())

    def after_lcdc_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        captured = self._capture_render_state(preserve_latched_scx_low=True)
        old_lcdc = self._line_lcdc_write_old_value
        self._line_lcdc_write_old_value = None
        window_enable_write_index: int | None = None
        if old_lcdc is not None and (old_lcdc ^ captured.lcdc) & LCDC_WINDOW_ENABLE:
            window_enable_write_index = self._line_lcdc_window_enable_write_count
            self._line_lcdc_window_enable_write_count += 1
        visible_x = self._visible_pixels_emitted()
        previous = self._state_at_render_x(min(visible_x, SCREEN_WIDTH - 1))
        line_state = captured
        if self._window_trigger_missed_by_new_state(previous, captured, visible_x):
            line_state = replace(captured, lcdc=captured.lcdc & ~LCDC_WINDOW_ENABLE)

        self._line_lcdc_write_serial += 1
        if (previous.lcdc ^ line_state.lcdc) & LCDC_OBJ_ENABLE:
            self._remember_incurred_obj_penalties(visible_x)
        self._apply_lcdc_immediate_bits_from(
            visible_x,
            line_state,
            window_enable_write_index,
        )
        self._apply_lcdc_fetch_source_updates(visible_x, line_state)
        self._apply_lcdc_tile_data_byte_source_updates(visible_x, line_state)

        self._sync_segmented_window_penalty()
        self._sync_segmented_obj_penalty()

    def after_render_register_write(self, register_offset: int | None = None) -> None:
        self._note_line_zero_palette_phase_write(register_offset)
        if self._line_render_state is None:
            return
        if self.mode == MODE_HBLANK and self._line_render_complete:
            self._apply_hblank_palette_write()
            return
        if self.mode != MODE_DRAWING:
            return
        captured = self._capture_render_state(preserve_latched_scx_low=True)
        visible_x = self._palette_register_write_x()
        line_state = captured
        if self._window_trigger_missed_by_new_state(self._line_render_state, captured, visible_x):
            line_state = replace(captured, lcdc=captured.lcdc & ~LCDC_WINDOW_ENABLE)
        previous = self._state_at_render_x(min(visible_x, SCREEN_WIDTH - 1))
        if register_offset == BGP_REGISTER_OFFSET:
            visible_x = self._obj_clamped_palette_write_x(visible_x, previous, line_state)
            if self._scanline == 0 and visible_x > 0:
                obj_events = self._segmented_obj_penalty_events(self._scanline)
                if obj_events and obj_events[0][0] == 0:
                    visible_x = min(SCREEN_WIDTH, visible_x + 4)
            if (
                self._line_mode3_start_scroll_write_offset
                and previous.bgp == 0
                and line_state.bgp == 0xFF
            ):
                visible_x = min(
                    SCREEN_WIDTH,
                    visible_x + self._line_mode3_start_scroll_write_offset,
                )
            previous = self._state_at_render_x(min(visible_x, SCREEN_WIDTH - 1))
        if (
            (previous.bgp ^ line_state.bgp) & 0x03
            and not self._window_visible_on_line(previous)
            and not self._window_visible_on_line(line_state)
            and visible_x > 0
            and visible_x < SCREEN_WIDTH
        ):
            glitch_bgp = (line_state.bgp & ~0x03) | ((previous.bgp | line_state.bgp) & 0x03)
            glitch_state = replace(
                line_state,
                bgp=glitch_bgp,
            )
            self._apply_render_state_fields_from(
                visible_x,
                glitch_state,
                ("lcdc", "bgp", "obp0", "obp1"),
            )
            if visible_x + 1 < SCREEN_WIDTH:
                self._apply_render_state_fields_from(
                    visible_x + 1,
                    line_state,
                    ("lcdc", "bgp", "obp0", "obp1"),
                )
        else:
            self._apply_render_state_fields_from(
                visible_x,
                line_state,
                ("lcdc", "bgp", "obp0", "obp1"),
            )
        self._sync_segmented_window_penalty()
        self._sync_segmented_obj_penalty()

    def _raster_register_write_x(self) -> int:
        return max(
            0,
            min(
                SCREEN_WIDTH,
                self._visible_pixels_emitted() + RASTER_REGISTER_WRITE_PIXEL_OFFSET,
            ),
        )

    def _palette_register_write_x(self) -> int:
        raster_x = self._raster_register_write_x()
        if self._line_render_state is None:
            return raster_x

        if (
            self._line_window_enable_early_pulse
            and not self.bus.io[0x40] & LCDC_WINDOW_ENABLE
        ):
            return raster_x

        state = (
            self._line_render_segments[0][1]
            if self._line_render_segments is not None
            else self._line_render_state
        )
        unpenalized_x = max(
            0,
            min(
                SCREEN_WIDTH,
                self.line_dots
                - MODE2_DOTS
                - (12 + (state.scx & 0x07))
                + RASTER_REGISTER_WRITE_PIXEL_OFFSET,
            ),
        )
        if not self._window_visible_on_line(
            self._state_at_render_x(min(unpenalized_x, SCREEN_WIDTH - 1))
        ):
            if self._line_palette_phase_offset:
                return max(
                    raster_x,
                    max(
                        0,
                        min(
                            SCREEN_WIDTH,
                            self.line_dots
                            - (MODE2_DOTS - self._line_palette_phase_offset)
                            - (12 + (state.scx & 0x07))
                            + RASTER_REGISTER_WRITE_PIXEL_OFFSET,
                        ),
                    ),
                )
            return raster_x
        min_restart_x = max(0, -RASTER_REGISTER_WRITE_PIXEL_OFFSET)
        for event_x, _penalty in self._segmented_window_penalty_events():
            restart_x = min(unpenalized_x, max(min_restart_x, event_x))
            raster_x = max(raster_x, restart_x)
        return raster_x

    def _hblank_palette_register_write_x(self) -> int:
        if self._line_render_state is None:
            return self._palette_register_write_x()

        state = (
            self._line_render_segments[0][1]
            if self._line_render_segments is not None
            else self._line_render_state
        )
        penalty = sum(
            event_penalty
            for _event_x, event_penalty in self._segmented_mode3_penalty_events(self._scanline)
        )
        return max(
            0,
            min(
                SCREEN_WIDTH,
                self.line_dots
                - MODE2_DOTS
                - (12 + (state.scx & 0x07))
                - penalty
                + RASTER_REGISTER_WRITE_PIXEL_OFFSET,
            ),
        )

    def _obj_clamped_palette_write_x(
        self,
        visible_x: int,
        previous: PPURenderState,
        line_state: PPURenderState,
    ) -> int:
        if not (previous.bgp ^ line_state.bgp) & 0x03:
            return visible_x
        if self._window_visible_on_line(previous) or self._window_visible_on_line(line_state):
            return visible_x
        if self._line_render_state is None:
            return visible_x

        state = (
            self._line_render_segments[0][1]
            if self._line_render_segments is not None
            else self._line_render_state
        )
        unpenalized_x = max(
            0,
            min(
                SCREEN_WIDTH,
                self.line_dots
                - MODE2_DOTS
                - (12 + (state.scx & 0x07))
                + RASTER_REGISTER_WRITE_PIXEL_OFFSET,
            ),
        )
        adjusted_x = visible_x
        for event_x, _penalty in self._segmented_obj_penalty_events(self._scanline):
            if event_x <= 0:
                continue
            target_x = min(unpenalized_x, event_x)
            if adjusted_x >= target_x:
                continue
            glitch_low = (previous.bgp | line_state.bgp) & 0x03
            final_low = line_state.bgp & 0x03
            if glitch_low != final_low:
                target_x = max(0, target_x - 1)
            adjusted_x = max(adjusted_x, target_x)
        return adjusted_x

    def _apply_lcdc_immediate_bits_from(
        self,
        visible_x: int,
        line_state: PPURenderState,
        window_enable_write_index: int | None = None,
    ) -> None:
        if self._line_render_segments is None:
            return

        segments = list(self._line_render_segments)

        def source_lcdc_at(x: int) -> int:
            state = segments[0][1]
            for segment_x, segment_state in segments:
                if segment_x > x:
                    break
                state = segment_state
            return state.lcdc

        obj_updates = (
            (LCDC_OBJ_ENABLE, max(self._line_render_x, visible_x - 2)),
            (LCDC_OBJ_SIZE, max(self._line_render_x, visible_x)),
        )
        for obj_mask, obj_start_x in obj_updates:
            if visible_x <= 3:
                obj_start_x = self._line_render_x
            if not (source_lcdc_at(obj_start_x) ^ line_state.lcdc) & obj_mask:
                continue
            obj_change_points = [obj_start_x]
            obj_change_points.extend(x for x, _state in segments if x > obj_start_x)
            for start_x in obj_change_points:
                previous_lcdc = self._state_at_render_x(
                    min(max(start_x, 0), SCREEN_WIDTH - 1)
                ).lcdc
                lcdc = (previous_lcdc & ~obj_mask) | (line_state.lcdc & obj_mask)
                self._apply_render_state_fields_from(
                    start_x,
                    replace(line_state, lcdc=lcdc),
                    ("lcdc",),
                )

        change_points = [visible_x]
        change_points.extend(x for x, _state in segments if x > visible_x)
        for start_x in change_points:
            deferred_lcdc_bits = (
                LCDC_FETCH_SOURCE_BITS
                | LCDC_BG_WINDOW_ENABLE
                | LCDC_WINDOW_ENABLE
            )
            lcdc = (
                line_state.lcdc
                & ~deferred_lcdc_bits
            ) | (
                source_lcdc_at(start_x)
                & deferred_lcdc_bits
            )
            self._apply_render_state_fields_from(
                start_x,
                replace(line_state, lcdc=lcdc),
                ("lcdc",),
            )
        self._apply_lcdc_bg_window_enable_update(
            visible_x,
            line_state,
            segments,
            source_lcdc_at,
        )
        self._apply_lcdc_window_enable_update(
            visible_x,
            line_state,
            segments,
            source_lcdc_at,
            window_enable_write_index,
        )

    def _apply_lcdc_bg_window_enable_update(
        self,
        visible_x: int,
        line_state: PPURenderState,
        segments: list[tuple[int, PPURenderState]],
        source_lcdc_at,
    ) -> None:
        sampled_x = min(max(visible_x, 0), SCREEN_WIDTH - 1)
        if not (source_lcdc_at(sampled_x) ^ line_state.lcdc) & LCDC_BG_WINDOW_ENABLE:
            return

        write_index = self._line_lcdc_bg_enable_write_count
        self._line_lcdc_bg_enable_write_count += 1
        start_x, allow_backpatch = self._lcdc_bg_window_enable_update_x(
            visible_x,
            line_state,
            write_index,
        )

        change_points = [start_x]
        change_points.extend(x for x, _state in segments if x > start_x)
        for change_x in change_points:
            previous_lcdc = self._state_at_render_x(
                min(max(change_x, 0), SCREEN_WIDTH - 1)
            ).lcdc
            lcdc = (
                previous_lcdc & ~LCDC_BG_WINDOW_ENABLE
            ) | (line_state.lcdc & LCDC_BG_WINDOW_ENABLE)
            self._apply_render_state_fields_from(
                change_x,
                replace(line_state, lcdc=lcdc),
                ("lcdc",),
                clamp_to_render_x=not allow_backpatch,
            )
        if allow_backpatch and start_x < self._line_render_x:
            rendered_x = self._line_render_x
            self._rerender_completed_line_from(start_x)
            self._line_render_x = rendered_x

    def _lcdc_bg_window_enable_update_x(
        self,
        visible_x: int,
        line_state: PPURenderState,
        write_index: int,
    ) -> tuple[int, bool]:
        events = self._segmented_obj_penalty_events(self._scanline)
        first_event_x, first_event_penalty = events[0] if events else (None, None)

        if (
            self._scanline == 0
            and first_event_x == 0
            and first_event_penalty == 10
            and write_index < 4
        ):
            if write_index == 0:
                return 0, False
            return [10, 19, 26][write_index - 1], False

        fully_off_left_sprite = self._line_has_fully_off_left_sprite()

        if write_index == 0:
            if fully_off_left_sprite or (first_event_x == 0 and first_event_penalty == 9):
                return 0, False
            if first_event_x is not None and first_event_x > 0:
                return first_event_x, False
            if first_event_x is not None:
                return max(0, visible_x - 2), False
            return visible_x, False

        turning_on = bool(line_state.lcdc & LCDC_BG_WINDOW_ENABLE)
        offset = -3 if turning_on else -2
        allow_backpatch = False
        if fully_off_left_sprite:
            offset -= 1
            allow_backpatch = True
        return max(0, min(SCREEN_WIDTH, visible_x + offset)), allow_backpatch

    def _apply_lcdc_window_enable_update(
        self,
        visible_x: int,
        line_state: PPURenderState,
        segments: list[tuple[int, PPURenderState]],
        source_lcdc_at,
        window_enable_write_index: int | None,
    ) -> None:
        if window_enable_write_index is None or self._line_render_segments is None:
            return

        desired_window_enable = line_state.lcdc & LCDC_WINDOW_ENABLE
        if (
            window_enable_write_index == 0
            and not desired_window_enable
            and visible_x <= 4
        ):
            self._line_window_enable_early_pulse = True

        if self._line_window_enable_early_pulse:
            if desired_window_enable:
                if line_state.wx < 44:
                    return
                start_x = max(0, line_state.wx - 7)
            else:
                start_x = self._early_lcdc_window_disable_x(
                    line_state.wx,
                    window_enable_write_index,
                )
            if start_x is None:
                return
            if start_x == 0 and (line_state.wx & 0x07) == 0x07:
                self._line_window_enable_cancel_glitch_x = max(0, line_state.wx - 7)
        else:
            sampled_x = min(max(visible_x, 0), SCREEN_WIDTH - 1)
            if not (source_lcdc_at(sampled_x) ^ line_state.lcdc) & LCDC_WINDOW_ENABLE:
                return
            previous = self._state_at_render_x(sampled_x)
            if desired_window_enable:
                start_x = self._next_window_fetch_boundary(self._line_render_x, previous)
            else:
                start_x = self._next_window_fetch_boundary(visible_x, previous)
                if (
                    self._scanline == 0
                    and window_enable_write_index == 0
                    and self._line_window_used
                ):
                    start_x = self._next_window_fetch_boundary(start_x + 1, previous)

        captured = line_state
        fields = ["lcdc"]
        if desired_window_enable:
            fields.append("wx")
        elif self._line_window_used:
            captured = replace(
                captured,
                scx=(line_state.scx & 0xF8) | ((-start_x) & 0x07),
            )
            fields.append("scx")
        change_points = [start_x]
        change_points.extend(x for x, _state in segments if x > start_x)
        for change_x in change_points:
            previous_lcdc = self._state_at_render_x(
                min(max(change_x, 0), SCREEN_WIDTH - 1)
            ).lcdc
            lcdc = (
                previous_lcdc & ~LCDC_WINDOW_ENABLE
            ) | desired_window_enable
            self._apply_render_state_fields_from(
                change_x,
                replace(captured, lcdc=lcdc),
                tuple(fields),
                clamp_to_render_x=False,
            )
        if start_x < self._line_render_x:
            rendered_x = self._line_render_x
            self._rerender_completed_line_from(start_x)
            self._line_render_x = rendered_x

    @staticmethod
    def _early_lcdc_window_disable_x(
        wx: int,
        window_enable_write_index: int,
    ) -> int | None:
        window_start_x = max(0, wx - 7)
        if window_enable_write_index == 0:
            hidden_end = PPU._hidden_edge_window_disable_x(wx)
            if hidden_end is not None:
                return hidden_end
            if 8 <= wx < 16:
                return 0
            if 16 <= wx < 22:
                return None
            if 22 <= wx < 30:
                return window_start_x + 16
            if 30 <= wx < 36:
                return window_start_x + 8
            if 36 <= wx < 44:
                return 0
            return window_start_x
        if window_enable_write_index == 2 and 16 <= wx < 22:
            return window_start_x + 24
        return None

    @staticmethod
    def _hidden_edge_window_disable_x(wx: int) -> int | None:
        if wx == 0:
            return 9
        if wx == 1:
            return 10
        if 2 <= wx <= 7:
            return wx + 1
        return None

    @staticmethod
    def _next_window_fetch_boundary(x: int, state: PPURenderState) -> int:
        x = max(0, min(SCREEN_WIDTH, x))
        window_start_x = max(0, state.wx - 7)
        if x <= window_start_x:
            return window_start_x
        pending_pixels = (8 - ((x - window_start_x) & 0x07)) & 0x07
        return min(SCREEN_WIDTH, x + pending_pixels)

    def _apply_lcdc_fetch_source_updates(
        self,
        visible_x: int,
        line_state: PPURenderState,
    ) -> None:
        updates: dict[int, int] = {}
        previous = self._state_at_render_x(min(max(visible_x, 0), SCREEN_WIDTH - 1))
        window_fetch_active = self._window_fetch_active_for_source_change(
            previous,
            line_state,
            visible_x,
        )
        fetch_groups = [
            (LCDC_BG_TILEMAP, LCDC_TILEMAP_WRITE_FETCH_OFFSET, False),
            (LCDC_WINDOW_TILEMAP, self._window_tilemap_write_fetch_offset(visible_x), True),
        ]
        if not window_fetch_active:
            fetch_groups.insert(
                0,
                (LCDC_BG_WINDOW_TILE_DATA, LCDC_TILE_DATA_WRITE_FETCH_OFFSET, False),
            )

        for mask, offset, window_fetch in fetch_groups:
            base_x = max(0, min(SCREEN_WIDTH, visible_x + offset))
            start_x = (
                self._window_lcdc_source_update_x(base_x, visible_x, previous, line_state, mask)
                if window_fetch
                else self._lcdc_fetch_source_update_x(base_x)
            )
            if (
                mask == LCDC_BG_WINDOW_TILE_DATA
                and not window_fetch
                and line_state.lcdc & mask
                and visible_x > 16
                and self._scanline == 0
                and self._has_very_early_positive_obj_fetch()
            ):
                start_x = self._next_bg_fetch_boundary(start_x + 1)
            previous_lcdc = self._state_at_render_x(min(start_x, SCREEN_WIDTH - 1)).lcdc
            if not (previous_lcdc ^ line_state.lcdc) & mask:
                continue
            updates[start_x] = updates.get(start_x, 0) | mask

        for start_x, mask in sorted(updates.items()):
            previous_lcdc = self._state_at_render_x(min(start_x, SCREEN_WIDTH - 1)).lcdc
            current_lcdc = (
                line_state.lcdc & ~LCDC_FETCH_SOURCE_BITS
            ) | (previous_lcdc & LCDC_FETCH_SOURCE_BITS)
            current_lcdc = (current_lcdc & ~mask) | (line_state.lcdc & mask)
            self._apply_render_state_fields_from(
                start_x,
                replace(line_state, lcdc=current_lcdc),
                ("lcdc",),
            )

    def _lcdc_fetch_source_update_x(self, base_x: int) -> int:
        start_x = self._next_bg_fetch_boundary(base_x)
        for event_x, penalty in self._segmented_mode3_penalty_events(self._scanline):
            if event_x < start_x <= event_x + 8 and (event_x > 0 or penalty > 10):
                return self._next_bg_fetch_boundary(start_x + 1)
        return start_x

    def _lcdc_tile_data_source_update_x(self, base_x: int, visible_x: int) -> int:
        start_x = self._next_bg_fetch_boundary(base_x)
        for event_x, penalty in self._segmented_mode3_penalty_events(self._scanline):
            if (
                visible_x > event_x
                and event_x < start_x <= event_x + 8
                and (event_x > 0 or penalty > 10)
            ):
                return self._next_bg_fetch_boundary(start_x + 1)
        return start_x

    def _lcdc_tile_data_high_source_update_x(
        self,
        base_x: int,
        visible_x: int,
        *,
        defer_exact_boundary: bool = False,
    ) -> int:
        start_x = self._next_bg_fetch_boundary(base_x)
        if defer_exact_boundary and base_x == start_x and base_x > 0:
            start_x = self._next_bg_fetch_boundary(start_x + 1)
        for event_x, penalty in self._segmented_mode3_penalty_events(self._scanline):
            if not (event_x < start_x <= event_x + 8 and (event_x > 0 or penalty > 10)):
                continue
            if start_x == 8 or visible_x > event_x:
                return self._next_bg_fetch_boundary(start_x + 1)
        return start_x

    def _apply_lcdc_tile_data_byte_source_updates(
        self,
        visible_x: int,
        line_state: PPURenderState,
    ) -> None:
        if self._line_bg_tile_data_sources is None:
            return

        desired_source = line_state.lcdc & LCDC_BG_WINDOW_TILE_DATA
        low_base_x = max(
            0,
            min(SCREEN_WIDTH, visible_x + LCDC_TILE_DATA_WRITE_FETCH_OFFSET),
        )
        high_base_x = max(0, min(SCREEN_WIDTH, visible_x + 1))
        low_x = self._lcdc_tile_data_source_update_x(low_base_x, visible_x)
        high_x = self._lcdc_tile_data_high_source_update_x(
            high_base_x,
            visible_x,
            defer_exact_boundary=True,
        )
        if desired_source == 0 and high_x < low_x and high_x < 16:
            high_x = low_x
        previous = self._state_at_render_x(min(max(visible_x, 0), SCREEN_WIDTH - 1))
        window_fetch_active = self._window_fetch_active_for_source_change(
            previous,
            line_state,
            visible_x,
        )
        if window_fetch_active:
            if visible_x <= 9:
                if desired_source:
                    self._apply_initial_window_tile_data_source_pulse(desired_source)
                return
            low_base_x = max(
                0,
                min(
                    SCREEN_WIDTH,
                    visible_x + LCDC_WINDOW_TILE_DATA_LOW_WRITE_FETCH_OFFSET,
                ),
            )
            high_base_x = max(
                0,
                min(
                    SCREEN_WIDTH,
                    visible_x + LCDC_WINDOW_TILE_DATA_HIGH_WRITE_FETCH_OFFSET,
                ),
            )
            low_x = self._window_lcdc_source_update_x(
                low_base_x,
                visible_x,
                previous,
                line_state,
                LCDC_BG_WINDOW_TILE_DATA,
            )
            high_x = self._window_lcdc_source_update_x(
                high_base_x,
                visible_x,
                previous,
                line_state,
                LCDC_BG_WINDOW_TILE_DATA,
            )
            low_x = self._defer_window_tile_data_byte_source_claim_collision(
                low_base_x,
                low_x,
                0,
                desired_source,
            )
            high_x = self._defer_window_tile_data_byte_source_claim_collision(
                high_base_x,
                high_x,
                1,
                desired_source,
            )
            self._claim_window_tile_data_byte_source_update(low_x, 0)
            self._claim_window_tile_data_byte_source_update(high_x, 1)
        early_obj_tile_data_claim = (
            not window_fetch_active
            and self._scanline == 0
            and self._has_very_early_positive_obj_fetch()
        )
        if early_obj_tile_data_claim and desired_source and visible_x > 16:
            low_x = self._next_bg_fetch_boundary(low_x + 1)
            high_x = self._next_bg_fetch_boundary(high_x + 1)
        if early_obj_tile_data_claim:
            low_x = self._defer_lcdc_tile_data_byte_source_claim_collision(
                low_x,
                0,
            )
            high_x = self._defer_lcdc_tile_data_byte_source_claim_collision(
                high_x,
                1,
            )
        else:
            low_x = self._defer_lcdc_tile_data_byte_source_collision(
                low_base_x,
                low_x,
                0,
                desired_source,
            )
            high_x = self._defer_lcdc_tile_data_byte_source_collision(
                high_base_x,
                high_x,
                1,
                desired_source,
            )
        if early_obj_tile_data_claim and desired_source:
            self._claim_lcdc_tile_data_byte_source_update(low_x, 0, desired_source)
            self._claim_lcdc_tile_data_byte_source_update(high_x, 1, desired_source)
        else:
            self._set_bg_tile_data_byte_source(low_x, 0, desired_source)
            self._set_bg_tile_data_byte_source(high_x, 1, desired_source)
        previous_source = previous.lcdc & LCDC_BG_WINDOW_TILE_DATA
        if not window_fetch_active and self._tile_data_low_write_hit_fetch_boundary(
            visible_x,
            low_x,
            previous_source,
            desired_source,
        ):
            self._set_bg_tile_data_byte_source(low_x, 0, previous_source)

    def _tile_data_low_write_hit_fetch_boundary(
        self,
        visible_x: int,
        low_x: int,
        previous_source: int,
        desired_source: int,
    ) -> bool:
        if previous_source == desired_source:
            return False
        base_x = max(
            0,
            min(SCREEN_WIDTH, visible_x + LCDC_TILE_DATA_WRITE_FETCH_OFFSET),
        )
        if base_x == 0 or base_x != low_x or base_x != self._next_bg_fetch_boundary(base_x):
            return False
        return any(
            0 < event_x < base_x
            for event_x, _penalty in self._segmented_obj_penalty_events(self._scanline)
        )

    def _defer_lcdc_tile_data_byte_source_collision(
        self,
        base_x: int,
        x: int,
        byte_index: int,
        desired_source: int,
    ) -> int:
        if self._line_bg_tile_data_sources is None or x <= 0 or x >= SCREEN_WIDTH:
            return x
        if base_x >= x:
            return x
        tile_x = self._bg_tile_screen_x(x)
        current = self._line_bg_tile_data_sources.get(tile_x)
        if current is None:
            return x
        current_source = current[byte_index]
        if current_source is None:
            state = self._state_at_render_x(min(max(tile_x, 0), SCREEN_WIDTH - 1))
            current_source = state.lcdc & LCDC_BG_WINDOW_TILE_DATA
        if current_source == desired_source:
            return x
        return self._next_bg_fetch_boundary(x + 1)

    def _defer_lcdc_tile_data_byte_source_claim_collision(
        self,
        x: int,
        byte_index: int,
    ) -> int:
        if self._line_bg_tile_data_sources is None or x <= 0 or x >= SCREEN_WIDTH:
            return x

        tile_x = self._bg_tile_screen_x(x)
        claimed_by = self._line_lcdc_tile_data_source_claims.get((tile_x, byte_index))
        if claimed_by is None or claimed_by >= self._line_lcdc_write_serial:
            return x
        return self._next_bg_fetch_boundary(x + 1)

    def _defer_window_tile_data_byte_source_claim_collision(
        self,
        base_x: int,
        x: int,
        byte_index: int,
        desired_source: int,
    ) -> int:
        if self._line_bg_tile_data_sources is None or x <= 0 or x >= SCREEN_WIDTH:
            return x
        if base_x >= x:
            return x

        tile_x = self._bg_tile_screen_x(x)
        claimed_by = self._line_window_tile_data_source_claims.get((tile_x, byte_index))
        if claimed_by is None or claimed_by >= self._line_lcdc_write_serial:
            if desired_source == 0 and self._has_very_early_positive_obj_fetch():
                previous_tile_x = max(0, x - 8)
                previous_claimed_by = self._line_window_tile_data_source_claims.get(
                    (previous_tile_x, byte_index)
                )
                if (
                    previous_claimed_by is not None
                    and previous_claimed_by < self._line_lcdc_write_serial
                ):
                    return self._next_bg_fetch_boundary(x + 1)
            return x

        deferred_x = self._next_bg_fetch_boundary(x + 1)
        if desired_source == 0 and self._has_very_early_positive_obj_fetch():
            deferred_x = self._next_bg_fetch_boundary(deferred_x + 1)
        return deferred_x

    def _claim_window_tile_data_byte_source_update(self, x: int, byte_index: int) -> None:
        if self._line_bg_tile_data_sources is None or x < 0 or x >= SCREEN_WIDTH:
            return
        tile_x = self._bg_tile_screen_x(x)
        self._line_window_tile_data_source_claims[(tile_x, byte_index)] = (
            self._line_lcdc_write_serial
        )

    def _claim_lcdc_tile_data_byte_source_update(
        self,
        x: int,
        byte_index: int,
        source: int,
    ) -> None:
        if self._line_bg_tile_data_sources is None or x < 0 or x >= SCREEN_WIDTH:
            return
        tile_x = self._bg_tile_screen_x(x)
        self._line_lcdc_tile_data_source_claims[(tile_x, byte_index)] = (
            self._line_lcdc_write_serial
        )
        self._set_bg_tile_data_byte_source(x, byte_index, source, force=True)

    def _has_very_early_positive_obj_fetch(self) -> bool:
        events = self._segmented_obj_penalty_events(self._scanline)
        return any(event_x == 0 for event_x, _penalty in events) and any(
            0 < event_x <= 2 for event_x, _penalty in events
        )

    def _window_tilemap_write_fetch_offset(self, visible_x: int) -> int:
        if self._line_window_used and visible_x > 9:
            return 7
        return 0

    def _apply_initial_window_tile_data_source_pulse(self, source: int) -> None:
        low_starts, high_starts = self._initial_window_tile_data_source_pulse_starts()
        for tile_x in low_starts:
            self._claim_window_tile_data_byte_source_update(tile_x, 0)
            self._set_bg_tile_data_byte_source(tile_x, 0, source)
        for tile_x in high_starts:
            self._claim_window_tile_data_byte_source_update(tile_x, 1)
            self._set_bg_tile_data_byte_source(tile_x, 1, source)

    def _initial_window_tile_data_source_pulse_starts(
        self,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        selected = self._sprites_on_line(self.bus.io[0x40], self._scanline)
        selected.sort(key=lambda sprite: (sprite[0], sprite[1]))
        if not selected:
            return (0,), (0,)
        has_positive_sprite_fetch = any(sprite_x > 0 for sprite_x, *_rest in selected)
        for sprite_x, _index, _sprite_y, _tile_id, _attrs, raw_x in selected:
            if raw_x <= 2:
                if has_positive_sprite_fetch:
                    return (16,), (16,)
                return (0,), (0,)
            if 3 <= raw_x <= 4 and sprite_x < 0:
                return (0, 8), (0,)
            if 5 <= raw_x < 8 and sprite_x < 0:
                return (8,), (0, 8)
            if sprite_x >= 0:
                if raw_x < 16:
                    return (), (8,)
                return (16,), (8,)
        return (), ()

    def _window_fetch_active_for_source_change(
        self,
        previous: PPURenderState,
        line_state: PPURenderState,
        visible_x: int,
    ) -> bool:
        if self._line_window_used:
            return True
        if not (
            self._window_visible_on_line(previous)
            or self._window_visible_on_line(line_state)
        ):
            return False
        window_start = max(0, min(previous.wx, line_state.wx) - 7)
        return visible_x <= window_start or visible_x < SCREEN_WIDTH

    def _window_lcdc_source_update_x(
        self,
        x: int,
        visible_x: int,
        previous: PPURenderState,
        line_state: PPURenderState,
        mask: int,
    ) -> int:
        window_start = max(0, min(previous.wx, line_state.wx) - 7)
        x = max(window_start, min(SCREEN_WIDTH, x))
        pending_pixels = (8 - ((x - window_start) & 0x07)) & 0x07
        start_x = min(SCREEN_WIDTH, x + pending_pixels)
        future_source_active = self._future_lcdc_source_active(visible_x, mask)
        turning_off = (
            (bool(previous.lcdc & mask) or future_source_active)
            and not bool(line_state.lcdc & mask)
        )
        pulse_range = self._initial_window_source_pulse_range(previous)
        if window_start == 0 and visible_x <= 9:
            if pulse_range is None:
                return SCREEN_WIDTH
            pulse_start, pulse_end = pulse_range
            return pulse_end if turning_off else pulse_start
        if not turning_off:
            return start_x
        if visible_x == 0:
            return min(SCREEN_WIDTH, max(start_x, window_start + 8))
        return start_x

    def _future_lcdc_source_active(self, visible_x: int, mask: int) -> bool:
        if self._line_render_segments is None:
            return False
        return any(
            x >= visible_x and bool(state.lcdc & mask)
            for x, state in self._line_render_segments
        )

    def _initial_window_source_pulse_range(
        self,
        state: PPURenderState,
    ) -> tuple[int, int] | None:
        selected = self._sprites_on_line(state.lcdc, self._scanline)
        selected.sort(key=lambda sprite: (sprite[0], sprite[1]))
        if not selected:
            return (0, 8)
        for sprite_x, _index, _sprite_y, _tile_id, _attrs, raw_x in selected:
            if raw_x == 0:
                return (0, 8)
            if 0 < raw_x <= 2 and sprite_x < 0:
                return (0, 16)
            if 3 <= raw_x < 8 and sprite_x < 0:
                return (8, 16)
            if sprite_x >= 0:
                if raw_x < 16:
                    return None
                return (16, 24)
        return None

    def _set_bg_tile_data_byte_source(
        self,
        x: int,
        byte_index: int,
        source: int,
        *,
        force: bool = False,
    ) -> None:
        if self._line_bg_tile_data_sources is None or x >= SCREEN_WIDTH:
            return
        tile_x = self._bg_tile_screen_x(x)
        state = self._state_at_render_x(min(max(tile_x, 0), SCREEN_WIDTH - 1))
        current = self._line_bg_tile_data_sources.get(tile_x, (None, None))
        current_source = current[byte_index]
        if current_source is None:
            current_source = state.lcdc & LCDC_BG_WINDOW_TILE_DATA
        if current_source == source and not force:
            return
        if byte_index == 0:
            updated = (source, current[1])
        else:
            updated = (current[0], source)
        self._line_bg_tile_data_sources[tile_x] = updated

    def before_scroll_register_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        self._line_pre_scroll_write_values = (self.bus.io[0x42], self.bus.io[0x43])
        self._render_active_line_until(self._visible_pixels_emitted())

    def after_scroll_register_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        visible_x = self._visible_pixels_emitted()
        previous = self._state_at_render_x(min(max(visible_x, 0), SCREEN_WIDTH - 1))
        early_wx_zero_window = (
            visible_x == 0
            and self._line_render_state.wx == 0
            and self._window_visible_on_line(self._line_render_state)
        )
        if early_wx_zero_window and self._scanline == 0 and self.line_dots == MODE2_DOTS:
            self._line_mode3_start_scroll_write_offset = 4
        preserve_latched_scx_low = (
            not early_wx_zero_window
            and self.line_dots >= SCX_LOW_BITS_LATCH_DOT
        )
        previous_scx_low = self._line_render_state.scx & 0x07
        captured = self._capture_render_state(
            preserve_latched_scx_low=preserve_latched_scx_low
        )
        old_scy, old_scx = self._line_pre_scroll_write_values or (previous.scy, previous.scx)
        self._line_pre_scroll_write_values = None
        old_effective_scx = old_scx
        if not early_wx_zero_window:
            old_effective_scx = (old_scx & 0xF8) | (self._line_render_state.scx & 0x07)
        if early_wx_zero_window:
            self._apply_render_state_fields_from(
                self._next_bg_fetch_boundary(visible_x),
                captured,
                ("scy", "scx"),
            )
            return

        if captured.scy != old_scy:
            if self._is_scy_bit2_pulse(old_scy, captured.scy):
                self._apply_scy_bit2_pulse_fetch_updates(visible_x, captured.scy)
                scy_x = self._scroll_scy_bit2_pulse_update_x(visible_x)
            else:
                self._apply_scy_fetch_updates(visible_x, captured.scy)
                scy_x = self._next_bg_fetch_boundary(visible_x)
            self._apply_render_state_fields_from(
                scy_x,
                captured,
                ("scy",),
            )
        if captured.scx != old_effective_scx:
            if not preserve_latched_scx_low:
                self._apply_scx_low_bits_mode3_delta(previous_scx_low, captured.scx & 0x07)
            self._apply_render_state_fields_from(
                self._scroll_scx_update_x(visible_x),
                captured,
                ("scx",),
            )

    def _apply_scx_low_bits_mode3_delta(self, previous_low: int, captured_low: int) -> None:
        if previous_low == captured_low:
            return
        self._line_mode3_dots = min(
            MODE3_MAX_DOTS,
            max(0, self._line_mode3_dots + captured_low - previous_low),
        )

    def _scroll_scx_update_x(self, visible_x: int) -> int:
        events = self._segmented_mode3_penalty_events(self._scanline)
        if not events:
            return self._next_bg_fetch_boundary(visible_x)

        offset = LCDC_TILEMAP_WRITE_FETCH_OFFSET
        if self._scanline == 0:
            offset = 8
        elif (
            any(event_x == 0 for event_x, _penalty in events)
            and any(0 < event_x <= 4 for event_x, _penalty in events)
        ):
            offset = 7
        return self._lcdc_fetch_source_update_x(max(0, min(SCREEN_WIDTH, visible_x + offset)))

    @staticmethod
    def _is_scy_bit2_pulse(old_scy: int, new_scy: int) -> bool:
        return (old_scy ^ new_scy) == 0x04

    def _scroll_scy_bit2_pulse_update_x(self, visible_x: int) -> int:
        if not self._segmented_mode3_penalty_events(self._scanline):
            return self._next_bg_fetch_boundary(visible_x)
        return self._lcdc_fetch_source_update_x(
            max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILEMAP_WRITE_FETCH_OFFSET))
        )

    def _apply_scy_bit2_pulse_fetch_updates(self, visible_x: int, scy: int) -> None:
        if self._line_bg_tile_map_scy is None or self._line_bg_tile_data_scy is None:
            return
        if visible_x == 0:
            self._apply_scy_fetch_updates(visible_x, scy)
            return

        map_x = self._lcdc_fetch_source_update_x(
            max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILEMAP_WRITE_FETCH_OFFSET))
        )
        low_x = self._lcdc_tile_data_source_update_x(
            max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILEMAP_WRITE_FETCH_OFFSET)),
            visible_x,
        )
        high_offset = (
            LCDC_TILEMAP_WRITE_FETCH_OFFSET
            if self._scanline == 0
            else LCDC_TILE_DATA_WRITE_FETCH_OFFSET
        )
        high_x = self._lcdc_tile_data_high_source_update_x(
            max(0, min(SCREEN_WIDTH, visible_x + high_offset)),
            visible_x,
        )
        self._set_bg_tile_map_scy(map_x, scy)
        self._set_bg_tile_data_scy(low_x, 0, scy)
        self._set_bg_tile_data_scy(high_x, 1, scy)

    def _apply_scy_fetch_updates(self, visible_x: int, scy: int) -> None:
        if self._line_bg_tile_map_scy is None or self._line_bg_tile_data_scy is None:
            return

        if visible_x == 0:
            if self._scanline == 0:
                # LY=0 takes the shorter line_0_fix branch in Mealybug's handler, so
                # its first visible SCY write lands in the first tile's data phase.
                fetch_group = max(0, (self.line_dots - (MODE2_DOTS + 4)) // 8)
                self._set_bg_tile_map_scy(self._lcdc_fetch_source_update_x(5), scy)
                if fetch_group == 0:
                    self._set_bg_tile_data_scy_both(0, scy)
                elif fetch_group != 1:
                    self._set_bg_tile_data_scy_both((fetch_group - 1) * 8, scy)
                return

            fetch_group = max(0, (self.line_dots - MODE2_DOTS) // 8)
            self._set_bg_tile_map_scy(fetch_group * 8, scy)
            self._set_bg_tile_map_scy(self._lcdc_fetch_source_update_x(5), scy)
            self._set_bg_tile_data_scy_both((fetch_group - 1) * 8, scy)
            return

        if self._scanline == 0:
            map_x = self._lcdc_fetch_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + 1))
            )
            low_x = self._lcdc_tile_data_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILE_DATA_WRITE_FETCH_OFFSET)),
                visible_x,
            )
            high_x = self._lcdc_tile_data_high_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILE_DATA_WRITE_FETCH_OFFSET)),
                visible_x,
            )
        else:
            map_x = self._lcdc_fetch_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILEMAP_WRITE_FETCH_OFFSET))
            )
            low_x = self._lcdc_tile_data_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILE_DATA_WRITE_FETCH_OFFSET)),
                visible_x,
            )
            high_x = self._lcdc_tile_data_high_source_update_x(
                max(0, min(SCREEN_WIDTH, visible_x + 1)),
                visible_x,
            )

        self._set_bg_tile_map_scy(map_x, scy)
        self._set_bg_tile_data_scy(low_x, 0, scy)
        self._set_bg_tile_data_scy(high_x, 1, scy)
        if self._scanline != 0 and visible_x < 5:
            self._set_bg_tile_data_scy_both(low_x, scy)
            # A wide OBJ fetch at x=8 leaves this BG map sample before the
            # source-update helper's usual sprite-stall skip point.
            if (
                self._scanline & 0x07 == 5
                and any(
                    event_x == 8 and penalty > 10
                    for event_x, penalty in self._segmented_mode3_penalty_events(self._scanline)
                )
            ):
                self._set_bg_tile_map_scy(
                    self._next_bg_fetch_boundary(
                        max(0, min(SCREEN_WIDTH, visible_x + LCDC_TILEMAP_WRITE_FETCH_OFFSET))
                    ),
                    scy,
                )

    def _set_bg_tile_map_scy(self, x: int, scy: int) -> None:
        if self._line_bg_tile_map_scy is None or x < 0 or x >= SCREEN_WIDTH:
            return
        self._line_bg_tile_map_scy[self._bg_tile_screen_x(x)] = scy

    def _set_bg_tile_data_scy(self, x: int, byte_index: int, scy: int) -> None:
        if self._line_bg_tile_data_scy is None or x < 0 or x >= SCREEN_WIDTH:
            return
        tile_x = self._bg_tile_screen_x(x)
        current = self._line_bg_tile_data_scy.get(tile_x, (None, None))
        if byte_index == 0:
            updated = (scy, current[1])
        else:
            updated = (current[0], scy)
        self._line_bg_tile_data_scy[tile_x] = updated

    def _set_bg_tile_data_scy_both(self, x: int, scy: int) -> None:
        self._set_bg_tile_data_scy(x, 0, scy)
        self._set_bg_tile_data_scy(x, 1, scy)

    def before_window_x_register_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        self._render_active_line_until(self._visible_pixels_emitted())

    def after_window_x_register_write(self) -> None:
        if self.mode != MODE_DRAWING or self._line_render_state is None:
            return
        captured = self._capture_render_state(preserve_latched_scx_low=True)
        visible_x = self._visible_pixels_emitted()
        previous = self._state_at_render_x(min(max(visible_x, 0), SCREEN_WIDTH - 1))
        window_start_x = max(0, captured.wx - 7)
        if self._preserve_started_hidden_edge_window(
            previous, captured, window_start_x, visible_x
        ):
            self._line_window_used = True
            self._line_window_start_glitch_x = None
            return
        if (
            visible_x == 0
            and not self._line_window_used
            and previous.wx in {4, 5, 6}
            and self._window_visible_on_line(previous)
            and self._window_visible_on_line(captured)
            and 0 < window_start_x < SCREEN_WIDTH
        ):
            self._line_window_start_glitch_x = window_start_x
        if self._window_fetch_started_before_wx_write(previous):
            self._line_window_used = True
            self._line_window_start_glitch_x = None
        if self._line_window_used:
            self._cancel_pending_window_reactivation_glitch(visible_x)
            return
        line_state = captured
        if self._window_hidden_edge_write_misses_trigger(previous, captured, visible_x):
            line_state = replace(captured, wx=167)
        elif window_start_x < visible_x:
            line_state = replace(captured, wx=167)
        self._apply_render_state_fields_from(visible_x, line_state, ("wx",))
        self._sync_segmented_window_penalty()

    def _preserve_started_hidden_edge_window(
        self,
        previous: PPURenderState,
        captured: PPURenderState,
        window_start_x: int,
        visible_x: int,
    ) -> bool:
        if (
            visible_x != 0
            or self._line_window_used
            or previous.wx not in {4, 5}
            or not self._window_visible_on_line(previous)
        ):
            return False

        if (
            self._window_visible_on_line(captured)
            and 0 <= window_start_x < SCREEN_WIDTH
            and (window_start_x - (previous.wx - 7)) % 8 == 0
        ):
            self._line_window_reactivation_glitch_x = window_start_x
        return True

    def _cancel_pending_window_reactivation_glitch(self, visible_x: int) -> None:
        glitch_x = self._line_window_reactivation_glitch_x
        if glitch_x is not None and visible_x <= glitch_x:
            self._line_window_reactivation_glitch_x = None

    def _window_fetch_started_before_wx_write(self, previous: PPURenderState) -> bool:
        if self._line_render_state is None or self._line_window_used:
            return False
        if not self._window_visible_on_line(previous) or previous.wx < 80:
            return False
        visible_x = self._visible_pixels_emitted()
        if visible_x <= 0:
            return False
        state = (
            self._line_render_segments[0][1]
            if self._line_render_segments is not None
            else self._line_render_state
        )
        unpenalized_x = max(
            0,
            min(
                SCREEN_WIDTH,
                self.line_dots - MODE2_DOTS - (12 + (state.scx & 0x07)),
            ),
        )
        return max(0, previous.wx - 7) <= unpenalized_x - 2

    def _window_hidden_edge_write_misses_trigger(
        self,
        previous: PPURenderState,
        captured: PPURenderState,
        visible_x: int,
    ) -> bool:
        return (
            visible_x == 0
            and previous.wx == 6
            and captured.wx < 6
            and self._window_visible_on_line(previous)
            and self._window_visible_on_line(captured)
        )

    def render_scanline(self, y: int, state: PPURenderState | None = None) -> None:
        if y >= SCREEN_HEIGHT:
            return
        state = state or self._capture_render_state()
        if not state.lcdc & LCDC_ENABLE:
            self.framebuffer[y] = [0 for _ in range(SCREEN_WIDTH)]
            return
        bg_color_ids = [0 for _ in range(SCREEN_WIDTH)]
        row = [0 for _ in range(SCREEN_WIDTH)]
        window_used = self._render_scanline_segment(
            state, y, row, bg_color_ids, 0, SCREEN_WIDTH
        )

        if window_used:
            self.window_line = (self.window_line + 1) & 0xFF

        self.framebuffer[y] = row

    def frame_as_ppm(self) -> str:
        lines = [f"P3\n{SCREEN_WIDTH} {SCREEN_HEIGHT}\n255"]
        for row in self.framebuffer:
            pixels: list[str] = []
            for shade in row:
                r, g, b = DMG_GRAYSCALE[shade & 0x03]
                pixels.append(f"{r} {g} {b}")
            lines.append(" ".join(pixels))
        return "\n".join(lines) + "\n"

    def write_frame_ppm(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.frame_as_ppm(), encoding="ascii")

    def frame_as_bmp(self) -> bytes:
        row_stride = (SCREEN_WIDTH * 3 + 3) & ~0x03
        pixel_data_size = row_stride * SCREEN_HEIGHT
        header_size = 14 + 40
        file_size = header_size + pixel_data_size
        data = bytearray()
        data.extend(b"BM")
        data.extend(file_size.to_bytes(4, "little"))
        data.extend((0).to_bytes(4, "little"))
        data.extend(header_size.to_bytes(4, "little"))
        data.extend((40).to_bytes(4, "little"))
        data.extend(SCREEN_WIDTH.to_bytes(4, "little", signed=True))
        data.extend(SCREEN_HEIGHT.to_bytes(4, "little", signed=True))
        data.extend((1).to_bytes(2, "little"))
        data.extend((24).to_bytes(2, "little"))
        data.extend((0).to_bytes(4, "little"))
        data.extend(pixel_data_size.to_bytes(4, "little"))
        data.extend((0).to_bytes(4, "little", signed=True))
        data.extend((0).to_bytes(4, "little", signed=True))
        data.extend((0).to_bytes(4, "little"))
        data.extend((0).to_bytes(4, "little"))

        padding = b"\x00" * (row_stride - SCREEN_WIDTH * 3)
        for row in reversed(self.framebuffer):
            for shade in row:
                red, green, blue = DMG_GRAYSCALE[shade & 0x03]
                data.extend((blue, green, red))
            data.extend(padding)
        return bytes(data)

    def write_frame_bmp(self, path: str | Path) -> None:
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(self.frame_as_bmp())

    def _advance_line(self) -> None:
        self._scanline += 1
        self._clear_line_rendering()
        if self._scanline == VISIBLE_LINES:
            self._set_ly(self._scanline)
            self._request_vblank_oam_stat_interrupt()
            self._set_mode(MODE_VBLANK)
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x01
        elif self._scanline >= LINES_PER_FRAME:
            self.frame_count += 1
            self._scanline = 0
            self.window_line = 0
            self._window_y_triggered = False
            self._set_ly(0)
            self._set_mode(MODE_OAM)
            self._check_window_y_trigger()
        else:
            self._set_ly(self._scanline)
            if self._scanline < VISIBLE_LINES:
                self._set_mode(MODE_OAM)
                self._check_window_y_trigger()
            else:
                self._set_mode(MODE_VBLANK)

    def _tile_data_address(self, lcdc: int, tile_id: int, y: int) -> int:
        if lcdc & LCDC_BG_WINDOW_TILE_DATA:
            return tile_id * 16 + y * 2
        signed_id = tile_id - 0x100 if tile_id & 0x80 else tile_id
        return 0x1000 + signed_id * 16 + y * 2

    def _tile_pixel(self, lcdc: int, tile_id: int, x: int, y: int) -> int:
        tile_address = self._tile_data_address(lcdc, tile_id, y)
        lo = self.bus.vram[tile_address & 0x1FFF]
        hi = self.bus.vram[(tile_address + 1) & 0x1FFF]
        bit = 7 - x
        return ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)

    def _tile_pixel_with_byte_sources(
        self,
        lcdc: int,
        tile_id: int,
        x: int,
        y: int,
        source_override: tuple[int | None, int | None] | None,
    ) -> int:
        if source_override is None:
            return self._tile_pixel(lcdc, tile_id, x, y)

        lo_source, hi_source = source_override
        lo_lcdc = (
            (lcdc & ~LCDC_BG_WINDOW_TILE_DATA) | lo_source
            if lo_source is not None
            else lcdc
        )
        hi_lcdc = (
            (lcdc & ~LCDC_BG_WINDOW_TILE_DATA) | hi_source
            if hi_source is not None
            else lcdc
        )
        lo_address = self._tile_data_address(lo_lcdc, tile_id, y)
        hi_address = self._tile_data_address(hi_lcdc, tile_id, y) + 1
        lo = self.bus.vram[lo_address & 0x1FFF]
        hi = self.bus.vram[hi_address & 0x1FFF]
        bit = 7 - x
        return ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)

    def _unsigned_tile_pixel(self, tile_id: int, x: int, y: int) -> int:
        tile_address = (tile_id & 0xFF) * 16 + y * 2
        lo = self.bus.vram[tile_address & 0x1FFF]
        hi = self.bus.vram[(tile_address + 1) & 0x1FFF]
        bit = 7 - x
        return ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)

    def _sprite_tile_byte(
        self,
        lcdc: int,
        y: int,
        sprite_y: int,
        tile_id: int,
        attrs: int,
        byte_index: int,
    ) -> int:
        sprite_height = 16 if lcdc & LCDC_OBJ_SIZE else 8
        sprite_row = y - sprite_y
        if attrs & OBJ_Y_FLIP:
            sprite_row = sprite_height - 1 - sprite_row
        if sprite_height == 16:
            tile_id = (tile_id & 0xFE) | ((sprite_row // 8) & 0x01)
        tile_y = sprite_row % 8
        address = (tile_id & 0xFF) * 16 + tile_y * 2 + byte_index
        return self.bus.vram[address & 0x1FFF]

    def _sprite_byte_lcdc(self, sprite_x: int, byte_offset: int, fetch_delay: int) -> int:
        # Left-clipped OBJ fetches are already in flight when x=0 LCDC writes land.
        if sprite_x < 0 and self._line_sprite_selection_lcdc is not None:
            return self._line_sprite_selection_lcdc
        sample_x = max(0, min(SCREEN_WIDTH - 1, sprite_x + byte_offset + fetch_delay))
        return self._state_at_render_x(sample_x).lcdc

    def _render_background_line(
        self,
        state: PPURenderState,
        y: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        bg_map_base = 0x1C00 if state.lcdc & LCDC_BG_TILEMAP else 0x1800
        scx = self._line_scx(state)
        if self._can_fast_render_tilemap():
            self._render_background_line_fast(
                state,
                y,
                row,
                bg_color_ids,
                start_x,
                end_x,
                bg_map_base,
                scx,
            )
            return

        for x in range(start_x, end_x):
            bg_x = (x + scx) & 0xFF
            bg_y = (y + state.scy) & 0xFF
            color_id = self._tilemap_pixel(
                state.lcdc,
                bg_map_base,
                bg_x,
                bg_y,
                screen_x=x,
            )
            bg_color_ids[x] = color_id
            row[x] = self._map_dmg_palette(state.bgp, color_id)

    def _render_window_line(
        self,
        state: PPURenderState,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        wx = state.wx - 7
        window_map_base = 0x1C00 if state.lcdc & LCDC_WINDOW_TILEMAP else 0x1800
        start_x = max(start_x, wx, 0)
        reactivation_glitch_x = (
            self._line_window_reactivation_glitch_x if state.wx in {4, 5} else None
        )
        if reactivation_glitch_x is None and self._can_fast_render_tilemap():
            self._render_window_line_fast(
                state,
                row,
                bg_color_ids,
                start_x,
                end_x,
                window_map_base,
                wx,
            )
            return

        for x in range(start_x, end_x):
            if x == reactivation_glitch_x:
                color_id = 0
            else:
                window_x = x - wx
                if reactivation_glitch_x is not None and x > reactivation_glitch_x:
                    window_x -= 1
                color_id = self._tilemap_pixel(
                    state.lcdc,
                    window_map_base,
                    window_x,
                    state.window_line,
                    screen_x=x,
                )
            bg_color_ids[x] = color_id
            row[x] = self._map_dmg_palette(state.bgp, color_id)

    def _can_fast_render_tilemap(self) -> bool:
        return (
            not self._line_bg_tile_map_scy
            and not self._line_bg_tile_data_sources
            and not self._line_bg_tile_data_scy
        )

    def _render_background_line_fast(
        self,
        state: PPURenderState,
        y: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
        map_base: int,
        scx: int,
    ) -> None:
        bg_y = (y + state.scy) & 0xFF
        tile_y = bg_y & 0x07
        map_row_base = map_base + ((bg_y >> 3) * 32)
        self._render_scrolled_tilemap_span_fast(
            state.lcdc,
            state.bgp,
            map_row_base,
            tile_y,
            scx,
            row,
            bg_color_ids,
            start_x,
            end_x,
        )

    def _render_window_line_fast(
        self,
        state: PPURenderState,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
        map_base: int,
        wx: int,
    ) -> None:
        tile_y = state.window_line & 0x07
        map_row_base = map_base + (((state.window_line & 0xFF) >> 3) * 32)
        self._render_unscrolled_tilemap_span_fast(
            state.lcdc,
            state.bgp,
            map_row_base,
            tile_y,
            wx,
            row,
            bg_color_ids,
            start_x,
            end_x,
        )

    def _render_scrolled_tilemap_span_fast(
        self,
        lcdc: int,
        palette: int,
        map_row_base: int,
        tile_y: int,
        scx: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        vram = self.bus.vram
        unsigned_tile_data = bool(lcdc & LCDC_BG_WINDOW_TILE_DATA)
        x = start_x
        while x < end_x:
            bg_x = (x + scx) & 0xFF
            span = min(end_x - x, 8 - (bg_x & 0x07))
            tile_id = vram[map_row_base + (bg_x >> 3)]
            if unsigned_tile_data:
                tile_address = tile_id * 16 + tile_y * 2
            else:
                signed_id = tile_id - 0x100 if tile_id & 0x80 else tile_id
                tile_address = 0x1000 + signed_id * 16 + tile_y * 2
            lo = vram[tile_address & 0x1FFF]
            hi = vram[(tile_address + 1) & 0x1FFF]
            bit_offset = bg_x & 0x07
            color_ids, shade_pixels = _decoded_tile_row(lo, hi, palette)
            if bit_offset == 0 and span == 8:
                bg_color_ids[x : x + 8] = color_ids
                row[x : x + 8] = shade_pixels
            else:
                span_end = bit_offset + span
                bg_color_ids[x : x + span] = color_ids[bit_offset:span_end]
                row[x : x + span] = shade_pixels[bit_offset:span_end]
            x += span

    def _render_unscrolled_tilemap_span_fast(
        self,
        lcdc: int,
        palette: int,
        map_row_base: int,
        tile_y: int,
        origin_x: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        vram = self.bus.vram
        unsigned_tile_data = bool(lcdc & LCDC_BG_WINDOW_TILE_DATA)
        x = start_x
        while x < end_x:
            tilemap_x = x - origin_x
            wrapped_x = tilemap_x & 0xFF
            span = min(end_x - x, 8 - (wrapped_x & 0x07))
            tile_id = vram[map_row_base + (wrapped_x >> 3)]
            if unsigned_tile_data:
                tile_address = tile_id * 16 + tile_y * 2
            else:
                signed_id = tile_id - 0x100 if tile_id & 0x80 else tile_id
                tile_address = 0x1000 + signed_id * 16 + tile_y * 2
            lo = vram[tile_address & 0x1FFF]
            hi = vram[(tile_address + 1) & 0x1FFF]
            bit_offset = wrapped_x & 0x07
            color_ids, shade_pixels = _decoded_tile_row(lo, hi, palette)
            if bit_offset == 0 and span == 8:
                bg_color_ids[x : x + 8] = color_ids
                row[x : x + 8] = shade_pixels
            else:
                span_end = bit_offset + span
                bg_color_ids[x : x + span] = color_ids[bit_offset:span_end]
                row[x : x + span] = shade_pixels[bit_offset:span_end]
            x += span

    def _render_sprites_line(
        self,
        state: PPURenderState,
        y: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        if self._sprites_hidden_by_dma(y):
            return

        selected = self._selected_sprites_for_line(state.lcdc, y)
        selected.sort(key=lambda sprite: (sprite[0], sprite[1]))
        fetch_delays: dict[int, int] = {}
        saw_fully_off_left_sprite = False
        for sprite_x, index, _sprite_y, _tile_id, _attrs, raw_x in selected:
            # A fully off-left OBJ fetch delays the next sprite's tile-data byte sampling.
            fetch_delays[index] = 1 if saw_fully_off_left_sprite else 0
            if raw_x == 0 and sprite_x < 0:
                saw_fully_off_left_sprite = True

        prepared_sprites: list[tuple[int, int, int, int, int, tuple[int, int, int, int]]] = []
        for sprite_x, index, sprite_y, tile_id, attrs, _raw_x in selected:
            fetch_delay = fetch_delays.get(index, 0)
            lo = self._sprite_tile_byte(
                self._sprite_byte_lcdc(sprite_x, 0, fetch_delay),
                y,
                sprite_y,
                tile_id,
                attrs,
                0,
            )
            hi = self._sprite_tile_byte(
                self._sprite_byte_lcdc(sprite_x, 2, fetch_delay),
                y,
                sprite_y,
                tile_id,
                attrs,
                1,
            )
            palette = state.obp1 if attrs & OBJ_DMG_PALETTE else state.obp0
            shades = (
                palette & 0x03,
                (palette >> 2) & 0x03,
                (palette >> 4) & 0x03,
                (palette >> 6) & 0x03,
            )
            prepared_sprites.append((sprite_x, sprite_x + 8, attrs, lo, hi, shades))

        for x in range(start_x, end_x):
            for sprite_x, sprite_right, attrs, lo, hi, shades in prepared_sprites:
                if not sprite_x <= x < sprite_right:
                    continue
                tile_x = x - sprite_x
                if attrs & OBJ_X_FLIP:
                    tile_x = 7 - tile_x
                bit = 7 - tile_x
                color_id = ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)
                if color_id == 0:
                    continue
                if attrs & OBJ_PRIORITY and bg_color_ids[x] != 0:
                    break
                row[x] = shades[color_id]
                break

    def _tilemap_pixel(
        self,
        lcdc: int,
        map_base: int,
        x: int,
        y: int,
        *,
        screen_x: int | None = None,
    ) -> int:
        needs_tile_x = screen_x is not None and (
            bool(self._line_bg_tile_map_scy)
            or bool(self._line_bg_tile_data_sources)
            or bool(self._line_bg_tile_data_scy)
        )
        tile_x = self._bg_tile_screen_x(screen_x) if needs_tile_x else None
        map_y = y
        if tile_x is not None and self._line_bg_tile_map_scy:
            map_scy = self._line_bg_tile_map_scy.get(tile_x)
            if map_scy is not None:
                map_y = (self._scanline + map_scy) & 0xFF
        tile_map_index = map_base + ((map_y & 0xFF) // 8) * 32 + ((x & 0xFF) // 8)
        tile_id = self.bus.vram[tile_map_index]
        source_override = None
        if tile_x is not None and self._line_bg_tile_data_sources:
            source_override = self._line_bg_tile_data_sources.get(tile_x)
        scy_override = None
        if tile_x is not None and self._line_bg_tile_data_scy:
            scy_override = self._line_bg_tile_data_scy.get(tile_x)
        if scy_override is not None:
            return self._tile_pixel_with_byte_sources_and_rows(
                lcdc,
                tile_id,
                x % 8,
                y % 8,
                source_override,
                scy_override,
            )
        return self._tile_pixel_with_byte_sources(
            lcdc,
            tile_id,
            x % 8,
            y % 8,
            source_override,
        )

    def _tile_pixel_with_byte_sources_and_rows(
        self,
        lcdc: int,
        tile_id: int,
        x: int,
        y: int,
        source_override: tuple[int | None, int | None] | None,
        scy_override: tuple[int | None, int | None],
    ) -> int:
        lo_scy, hi_scy = scy_override
        base_scy = (y - self._scanline) & 0xFF
        lo_y = (self._scanline + (lo_scy if lo_scy is not None else base_scy)) & 0xFF
        hi_y = (self._scanline + (hi_scy if hi_scy is not None else base_scy)) & 0xFF
        lo_source, hi_source = source_override or (None, None)
        lo_lcdc = (
            (lcdc & ~LCDC_BG_WINDOW_TILE_DATA) | lo_source
            if lo_source is not None
            else lcdc
        )
        hi_lcdc = (
            (lcdc & ~LCDC_BG_WINDOW_TILE_DATA) | hi_source
            if hi_source is not None
            else lcdc
        )
        lo_address = self._tile_data_address(lo_lcdc, tile_id, lo_y % 8)
        hi_address = self._tile_data_address(hi_lcdc, tile_id, hi_y % 8) + 1
        lo = self.bus.vram[lo_address & 0x1FFF]
        hi = self.bus.vram[hi_address & 0x1FFF]
        bit = 7 - x
        return ((hi >> bit) & 1) << 1 | ((lo >> bit) & 1)

    def _bg_tile_screen_x(self, x: int) -> int:
        state = self._state_at_render_x(min(max(x, 0), SCREEN_WIDTH - 1))
        return x - ((x + (state.scx & 0x07)) & 0x07)

    def _window_visible_on_line(self, state: PPURenderState) -> bool:
        if not state.lcdc & LCDC_WINDOW_ENABLE:
            return False
        return state.window_y_triggered and state.wx <= 166

    def _mode3_duration_for_line(self, state: PPURenderState, y: int) -> int:
        duration = MODE3_DOTS + (state.scx & 0x07)
        if (state.lcdc & LCDC_BG_WINDOW_ENABLE) and self._window_visible_on_line(state):
            duration += self._window_mode3_penalty(state)
        duration += self._obj_mode3_penalty(state, y)
        return min(duration, MODE3_MAX_DOTS)

    def _window_mode3_penalty(self, state: PPURenderState) -> int:
        penalty = 6
        if state.wx == 0 and (state.scx & 0x07):
            penalty += 1
        return penalty

    def _line_scx(self, state: PPURenderState) -> int:
        return state.scx

    def _sprites_on_line(self, lcdc: int, y: int) -> list[tuple[int, int, int, int, int, int]]:
        sprite_height = 16 if lcdc & LCDC_OBJ_SIZE else 8
        selected: list[tuple[int, int, int, int, int, int]] = []
        for index in range(40):
            offset = index * 4
            sprite_y_raw = self.bus.oam[offset]
            sprite_x_raw = self.bus.oam[offset + 1]
            sprite_y = sprite_y_raw - 16
            sprite_x = sprite_x_raw - 8
            if sprite_y <= y < sprite_y + sprite_height:
                selected.append(
                    (
                        sprite_x,
                        index,
                        sprite_y,
                        self.bus.oam[offset + 2],
                        self.bus.oam[offset + 3],
                        sprite_x_raw,
                    )
                )
                if len(selected) == MAX_SPRITES_PER_LINE:
                    break
        return selected

    def _selected_sprites_for_line(
        self,
        lcdc: int,
        y: int,
    ) -> list[tuple[int, int, int, int, int, int]]:
        if self._line_selected_sprites is not None and y == self._scanline:
            return list(self._line_selected_sprites)
        return self._sprites_on_line(lcdc, y)

    def _obj_mode3_penalty(self, state: PPURenderState, y: int) -> int:
        total = sum(penalty for _x, penalty in self._obj_penalty_events(state, y))
        return self._quantize_obj_mode3_penalty(total)

    def _segmented_obj_mode3_penalty(self, y: int) -> int:
        return self._quantize_obj_mode3_penalty(
            sum(penalty for _event_x, penalty in self._segmented_obj_penalty_events(y))
        )

    def _segmented_obj_penalty_events(self, y: int) -> list[tuple[int, int]]:
        if self._line_render_segments is None:
            if self._line_render_state is None:
                return []
            events = self._obj_penalty_events(self._line_render_state, y)
            return self._with_forced_obj_penalty_events(events)

        events: list[tuple[int, int]] = []
        for index, (start_x, state) in enumerate(self._line_render_segments):
            end_x = (
                self._line_render_segments[index + 1][0]
                if index + 1 < len(self._line_render_segments)
                else SCREEN_WIDTH
            )
            events.extend(
                (event_x, penalty)
                for event_x, penalty in self._obj_penalty_events(state, y)
                if start_x <= event_x < end_x
            )
        events.sort(key=lambda event: event[0])
        return self._with_forced_obj_penalty_events(events)

    def _with_forced_obj_penalty_events(
        self,
        events: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        if not self._line_forced_obj_penalty_events:
            return events

        merged = list(events)
        seen = set(merged)
        for event in self._line_forced_obj_penalty_events:
            if event in seen:
                continue
            merged.append(event)
            seen.add(event)
        merged.sort(key=lambda event: event[0])
        return merged

    @staticmethod
    def _quantize_obj_mode3_penalty(total: int) -> int:
        return (total // 4) * 4

    def _segmented_window_mode3_penalty(self) -> int:
        return sum(penalty for _event_x, penalty in self._segmented_window_penalty_events())

    def _segmented_window_penalty_events(self) -> list[tuple[int, int]]:
        if self._line_render_segments is None:
            if self._line_render_state is None:
                return []
            return self._window_penalty_events(self._line_render_state)

        events: list[tuple[int, int]] = []
        for index, (start_x, state) in enumerate(self._line_render_segments):
            end_x = (
                self._line_render_segments[index + 1][0]
                if index + 1 < len(self._line_render_segments)
                else SCREEN_WIDTH
            )
            events.extend(
                (event_x, penalty)
                for event_x, penalty in self._window_penalty_events(state)
                if start_x <= event_x < end_x
            )
        events.sort(key=lambda event: event[0])
        return events

    def _sync_segmented_obj_penalty(self) -> None:
        if self._line_render_segments is None:
            return

        obj_penalty = self._segmented_obj_mode3_penalty(self._scanline)
        if obj_penalty == self._line_obj_penalty_dots:
            return

        self._line_mode3_dots = min(
            MODE3_MAX_DOTS,
            max(0, self._line_mode3_dots + obj_penalty - self._line_obj_penalty_dots),
        )
        self._line_obj_penalty_dots = obj_penalty

    def _remember_incurred_obj_penalties(self, visible_x: int) -> None:
        if self._line_render_state is None:
            return

        existing = set(self._line_forced_obj_penalty_events)
        for event_x, penalty in self._segmented_obj_penalty_events(self._scanline):
            if event_x > visible_x or (event_x, penalty) in existing:
                continue
            incurred = self._incurred_obj_penalty(event_x, penalty, visible_x)
            event = (event_x, incurred)
            if event in existing:
                continue
            self._line_forced_obj_penalty_events.append(event)
            existing.add(event)

    def _incurred_obj_penalty(self, event_x: int, penalty: int, visible_x: int) -> int:
        if event_x == visible_x and penalty > 6:
            return penalty - 6
        if event_x == 0 and penalty == 10 and self._line_has_fully_off_left_sprite():
            return 11
        return penalty

    def _line_has_fully_off_left_sprite(self) -> bool:
        if self._line_render_state is None:
            return False
        selected = self._selected_sprites_for_line(
            self._line_render_state.lcdc,
            self._scanline,
        )
        return any(
            raw_x == 0
            for _sprite_x, _index, _sprite_y, _tile_id, _attrs, raw_x in selected
        )

    def _sync_segmented_window_penalty(self) -> None:
        if self._line_render_segments is None:
            return

        window_penalty = self._segmented_window_mode3_penalty()
        if window_penalty == self._line_window_penalty_dots:
            return

        self._line_mode3_dots = min(
            MODE3_MAX_DOTS,
            max(0, self._line_mode3_dots + window_penalty - self._line_window_penalty_dots),
        )
        self._line_window_penalty_dots = window_penalty

    def _apply_hblank_palette_write(self) -> None:
        if (
            self._line_render_segments is None
            or self._line_row is None
            or self._line_bg_color_ids is None
            or self._scanline >= VISIBLE_LINES
        ):
            return

        start_x = self._hblank_palette_register_write_x()
        if start_x >= SCREEN_WIDTH:
            return

        captured = self._capture_render_state(preserve_latched_scx_low=True)
        previous = self._state_at_render_x(start_x)
        if not (previous.bgp ^ captured.bgp) & 0x03:
            return
        sprites = self._sprites_on_line(previous.lcdc | captured.lcdc, self._scanline)
        if any(sprite_x + 8 > start_x for sprite_x, *_rest in sprites):
            return
        if self._window_visible_on_line(previous) or self._window_visible_on_line(captured):
            return

        glitch_bgp = (captured.bgp & ~0x03) | ((previous.bgp | captured.bgp) & 0x03)
        glitch_state = replace(captured, bgp=glitch_bgp)
        self._apply_render_state_fields_from(
            start_x,
            glitch_state,
            ("bgp",),
            clamp_to_render_x=False,
        )
        if start_x + 1 < SCREEN_WIDTH:
            self._apply_render_state_fields_from(
                start_x + 1,
                captured,
                ("bgp",),
                clamp_to_render_x=False,
            )
        self._rerender_completed_line_from(start_x)

    def _rerender_completed_line_from(self, start_x: int) -> None:
        if (
            self._line_render_segments is None
            or self._line_row is None
            or self._line_bg_color_ids is None
        ):
            return
        render_x = max(0, min(SCREEN_WIDTH, start_x))
        while render_x < SCREEN_WIDTH:
            state = self._state_at_render_x(render_x)
            segment_end = min(SCREEN_WIDTH, self._next_segment_start_after(render_x))
            self._render_scanline_segment(
                state,
                self._scanline,
                self._line_row,
                self._line_bg_color_ids,
                render_x,
                segment_end,
            )
            render_x = segment_end
        self.framebuffer[self._scanline] = self._line_row

    def _obj_penalty_events(self, state: PPURenderState, y: int) -> list[tuple[int, int]]:
        if not state.lcdc & LCDC_OBJ_ENABLE or self._sprites_hidden_by_dma(y):
            return []

        events: list[tuple[int, int]] = []
        considered_tiles: set[tuple[str, int, int]] = set()
        selected = self._selected_sprites_for_line(state.lcdc, y)
        selected.sort(key=lambda sprite: (sprite[0], sprite[1]))
        for sprite_x, _index, _sprite_y, _tile_id, _attrs, raw_x in selected:
            if raw_x == 0:
                tile_key = ("off-left", y, 0)
                penalty = 6
                if tile_key not in considered_tiles:
                    considered_tiles.add(tile_key)
                    penalty += 4
                events.append((0, penalty))
                continue
            if raw_x >= SCREEN_WIDTH + 8:
                continue

            penalty = 6
            tile_key, pixels_right = self._obj_pixel_tile_context(state, y, sprite_x)
            if tile_key not in considered_tiles:
                considered_tiles.add(tile_key)
                penalty += max(0, pixels_right - 2)
            events.append((max(0, sprite_x), penalty))
        return events

    def _obj_pixel_tile_context(self, state: PPURenderState, y: int, x: int) -> tuple[tuple[str, int, int], int]:
        if (state.lcdc & LCDC_BG_WINDOW_ENABLE) and self._window_visible_on_line(state):
            wx = state.wx - 7
            if x >= wx:
                window_x = x - wx
                return ("window", state.window_line // 8, window_x // 8), 7 - (window_x & 0x07)

        bg_x = (x + self._line_scx(state)) & 0xFF
        bg_y = (y + state.scy) & 0xFF
        return ("background", bg_y // 8, bg_x // 8), 7 - (bg_x & 0x07)

    def _capture_render_state(self, *, preserve_latched_scx_low: bool = False) -> PPURenderState:
        scx = self.bus.io[0x43]
        if preserve_latched_scx_low and self._line_render_state is not None:
            scx = (scx & 0xF8) | (self._line_render_state.scx & 0x07)
        return PPURenderState(
            lcdc=self.bus.io[0x40],
            scy=self.bus.io[0x42],
            scx=scx,
            bgp=self.bus.io[0x47],
            obp0=self.bus.io[0x48],
            obp1=self.bus.io[0x49],
            wx=self.bus.io[0x4B],
            window_line=self.window_line,
            window_y_triggered=self._window_y_triggered,
        )

    def _start_mode3_line(self) -> None:
        self._line_oam_dma_seen = self._line_oam_dma_seen or self.bus.oam_dma_active
        if self._line_oam_dma_seen:
            self._line_oam_dma_hidden_x = 0
        self._line_render_state = self._capture_render_state()
        self._line_render_segments = [(0, self._line_render_state)]
        self._line_row = [0 for _ in range(SCREEN_WIDTH)]
        self._line_bg_color_ids = [0 for _ in range(SCREEN_WIDTH)]
        self._line_bg_tile_data_sources = {}
        self._line_bg_tile_map_scy = {}
        self._line_bg_tile_data_scy = {}
        self._line_lcdc_write_serial = 0
        self._line_lcdc_bg_enable_write_count = 0
        self._line_lcdc_window_enable_write_count = 0
        self._line_lcdc_write_old_value = None
        self._line_window_tile_data_source_claims = {}
        self._line_lcdc_tile_data_source_claims = {}
        self._line_sprite_selection_lcdc = self._line_render_state.lcdc
        self._line_selected_sprites = self._sprites_on_line(
            self._line_sprite_selection_lcdc,
            self._scanline,
        )
        self._line_render_x = 0
        self._line_render_complete = False
        self._line_window_used = False
        self._line_window_active_at_render_x = False
        self._line_window_activation_count = 0
        self._line_window_start_glitch_x = None
        self._line_window_reactivation_glitch_x = None
        self._line_window_enable_early_pulse = False
        self._line_window_enable_cancel_glitch_x = None
        self._line_forced_obj_penalty_events = []
        self._line_window_penalty_dots = self._segmented_window_mode3_penalty()
        self._line_obj_penalty_dots = self._segmented_obj_mode3_penalty(self._scanline)

    def _finish_mode3_line(self, y: int) -> None:
        if (
            self._line_render_state is None
            or self._line_row is None
            or self._line_bg_color_ids is None
        ):
            self.render_scanline(y)
            self._clear_line_rendering()
            return
        self._render_active_line_until(SCREEN_WIDTH)
        self.framebuffer[y] = self._line_row
        if self._line_window_activation_count:
            self.window_line = (
                self.window_line + self._line_window_activation_count
            ) & 0xFF
        elif self._line_window_used:
            self.window_line = (self.window_line + 1) & 0xFF
        self._line_render_complete = True

    def _note_line_zero_palette_phase_write(self, register_offset: int | None) -> None:
        if (
            register_offset == BGP_REGISTER_OFFSET
            and self.mode == MODE_OAM
            and self._scanline == 0
            and MODE2_DOTS - 4 <= self.line_dots < MODE2_DOTS
        ):
            self._line_palette_phase_offset = max(
                self._line_palette_phase_offset,
                MODE2_DOTS - self.line_dots,
            )

    def _render_active_line_until(self, target_x: int) -> None:
        if (
            self._line_render_state is None
            or self._line_render_segments is None
            or self._line_row is None
            or self._line_bg_color_ids is None
        ):
            return
        target_x = max(self._line_render_x, min(SCREEN_WIDTH, target_x))
        while self._line_render_x < target_x:
            state = self._state_at_render_x(self._line_render_x)
            segment_end = min(target_x, self._next_segment_start_after(self._line_render_x))
            state = self._window_activation_state_for_segment(
                state,
                self._line_render_x,
                segment_end,
            )
            self._line_window_used = (
                self._render_scanline_segment(
                    state,
                    self._scanline,
                    self._line_row,
                    self._line_bg_color_ids,
                    self._line_render_x,
                    segment_end,
                )
                or self._line_window_used
            )
            self._line_render_x = segment_end
        self._line_render_state = self._state_at_render_x(min(self._line_render_x, SCREEN_WIDTH - 1))

    def _window_activation_state_for_segment(
        self,
        state: PPURenderState,
        start_x: int,
        end_x: int,
    ) -> PPURenderState:
        window_active = (
            bool(state.lcdc & LCDC_BG_WINDOW_ENABLE)
            and self._window_visible_on_line(state)
            and end_x > max(0, state.wx - 7)
        )
        if not window_active:
            self._line_window_active_at_render_x = False
            return state

        if not self._line_window_active_at_render_x:
            window_line = (self.window_line + self._line_window_activation_count) & 0xFF
            self._line_window_activation_count += 1
        else:
            window_line = (
                self.window_line + self._line_window_activation_count - 1
            ) & 0xFF
        self._line_window_active_at_render_x = True
        return replace(state, window_line=window_line)

    def _render_scanline_segment(
        self,
        state: PPURenderState,
        y: int,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> bool:
        if start_x >= end_x:
            return False
        if not state.lcdc & LCDC_ENABLE:
            for x in range(start_x, end_x):
                row[x] = 0
                bg_color_ids[x] = 0
            return False

        window_used = False
        if state.lcdc & LCDC_BG_WINDOW_ENABLE:
            self._render_background_line(state, y, row, bg_color_ids, start_x, end_x)
            if self._window_visible_on_line(state):
                wx = state.wx - 7
                window_start_x = max(0, wx)
                if end_x > window_start_x:
                    window_used = True
                    self._render_window_line(state, row, bg_color_ids, start_x, end_x)
        else:
            for x in range(start_x, end_x):
                row[x] = 0
                bg_color_ids[x] = 0

        self._apply_window_start_glitch(state, row, bg_color_ids, start_x, end_x)
        self._apply_window_enable_cancel_glitch(state, row, bg_color_ids, start_x, end_x)
        if state.lcdc & LCDC_OBJ_ENABLE:
            self._render_sprites_line(state, y, row, bg_color_ids, start_x, end_x)
        return window_used

    def _apply_window_start_glitch(
        self,
        state: PPURenderState,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        glitch_x = self._line_window_start_glitch_x
        if (
            glitch_x is None
            or not start_x <= glitch_x < end_x
            or (state.wx & 0x07) != 4
            or bg_color_ids[glitch_x] != 3
        ):
            return
        bg_color_ids[glitch_x] = 0
        row[glitch_x] = self._map_dmg_palette(state.bgp, 0)

    def _apply_window_enable_cancel_glitch(
        self,
        state: PPURenderState,
        row: list[int],
        bg_color_ids: list[int],
        start_x: int,
        end_x: int,
    ) -> None:
        glitch_x = self._line_window_enable_cancel_glitch_x
        if glitch_x is None or not start_x <= glitch_x < end_x:
            return
        bg_color_ids[glitch_x] = 0
        row[glitch_x] = self._map_dmg_palette(state.bgp, 0)

    def _visible_pixels_emitted(self) -> int:
        if self._line_render_state is None:
            return 0
        state = (
            self._line_render_segments[0][1]
            if self._line_render_segments is not None
            else self._line_render_state
        )
        mode3_dots = self.line_dots - MODE2_DOTS
        initial_delay = 12 + (state.scx & 0x07)
        events = self._segmented_mode3_penalty_events(self._scanline)
        cumulative_penalty = 0
        event_index = 0
        emitted = 0
        for x in range(SCREEN_WIDTH):
            while event_index < len(events) and events[event_index][0] <= x:
                cumulative_penalty += events[event_index][1]
                event_index += 1
            if initial_delay + x + cumulative_penalty >= mode3_dots:
                break
            emitted = x + 1
        return emitted

    def _next_bg_fetch_boundary(self, x: int) -> int:
        x = max(0, min(SCREEN_WIDTH, x))
        if x == 0:
            return 0
        state = self._state_at_render_x(min(x, SCREEN_WIDTH - 1))
        pending_pixels = (8 - ((x + (state.scx & 0x07)) & 0x07)) & 0x07
        return min(SCREEN_WIDTH, x + pending_pixels)

    def _apply_render_state_fields_from(
        self,
        start_x: int,
        captured: PPURenderState,
        fields: tuple[str, ...],
        *,
        clamp_to_render_x: bool = True,
    ) -> None:
        if self._line_render_segments is None:
            return
        start_x = max(0, min(SCREEN_WIDTH, start_x))
        if clamp_to_render_x:
            start_x = max(self._line_render_x, start_x)
        if start_x >= SCREEN_WIDTH:
            return
        self._ensure_render_segment_start(start_x)
        updates = {field: getattr(captured, field) for field in fields}
        self._line_render_segments = [
            (x, replace(state, **updates) if x >= start_x else state)
            for x, state in self._line_render_segments
        ]
        self._compact_render_segments()
        self._line_render_state = self._state_at_render_x(min(self._line_render_x, SCREEN_WIDTH - 1))

    def _window_trigger_missed_by_new_state(
        self,
        previous: PPURenderState,
        captured: PPURenderState,
        visible_x: int,
    ) -> bool:
        if self._line_window_used:
            return False
        if self._window_visible_on_line(previous) or not self._window_visible_on_line(captured):
            return False
        return max(0, captured.wx - 7) < visible_x

    def _sprites_hidden_by_dma(self, y: int) -> bool:
        if self._line_render_state is not None and y == self._scanline:
            return self._line_oam_dma_hidden_x == 0
        return self.bus.oam_dma_active

    def _ensure_render_segment_start(self, start_x: int) -> None:
        if self._line_render_segments is None:
            return
        if any(x == start_x for x, _state in self._line_render_segments):
            return
        self._line_render_segments.append((start_x, self._state_at_render_x(start_x)))
        self._line_render_segments.sort(key=lambda segment: segment[0])

    def _compact_render_segments(self) -> None:
        if self._line_render_segments is None:
            return
        compacted: list[tuple[int, PPURenderState]] = []
        for x, state in sorted(self._line_render_segments, key=lambda segment: segment[0]):
            if compacted and compacted[-1][1] == state:
                continue
            compacted.append((x, state))
        self._line_render_segments = compacted

    def _state_at_render_x(self, x: int) -> PPURenderState:
        if self._line_render_segments is None:
            if self._line_render_state is None:
                return self._capture_render_state()
            return self._line_render_state
        state = self._line_render_segments[0][1]
        for segment_x, segment_state in self._line_render_segments:
            if segment_x > x:
                break
            state = segment_state
        return state

    def _next_segment_start_after(self, x: int) -> int:
        if self._line_render_segments is None:
            return SCREEN_WIDTH
        for segment_x, _state in self._line_render_segments:
            if segment_x > x:
                return segment_x
        return SCREEN_WIDTH

    def _mode3_penalty_events(self, state: PPURenderState, y: int) -> list[tuple[int, int]]:
        events = self._obj_penalty_events(state, y)
        events.extend(self._window_penalty_events(state))
        events.sort(key=lambda event: event[0])
        return events

    def _window_penalty_events(self, state: PPURenderState) -> list[tuple[int, int]]:
        if (state.lcdc & LCDC_BG_WINDOW_ENABLE) and self._window_visible_on_line(state):
            penalty = 6
            if state.wx == 0 and (state.scx & 0x07):
                penalty += 1
            return [(max(0, state.wx - 7), penalty)]
        return []

    def _segmented_mode3_penalty_events(self, y: int) -> list[tuple[int, int]]:
        if self._line_render_segments is None:
            if self._line_render_state is None:
                return []
            events = self._mode3_penalty_events(self._line_render_state, y)
            return self._with_forced_obj_penalty_events(events)

        events: list[tuple[int, int]] = []
        for index, (start_x, state) in enumerate(self._line_render_segments):
            end_x = (
                self._line_render_segments[index + 1][0]
                if index + 1 < len(self._line_render_segments)
                else SCREEN_WIDTH
            )
            events.extend(
                (event_x, penalty)
                for event_x, penalty in self._mode3_penalty_events(state, y)
                if start_x <= event_x < end_x
            )
        events.sort(key=lambda event: event[0])
        return self._with_forced_obj_penalty_events(events)

    def _clear_line_rendering(self) -> None:
        self._line_mode3_dots = MODE3_DOTS
        self._line_render_state = None
        self._line_render_segments = None
        self._line_row = None
        self._line_bg_color_ids = None
        self._line_bg_tile_data_sources = None
        self._line_bg_tile_map_scy = None
        self._line_bg_tile_data_scy = None
        self._line_lcdc_write_serial = 0
        self._line_lcdc_bg_enable_write_count = 0
        self._line_lcdc_window_enable_write_count = 0
        self._line_lcdc_write_old_value = None
        self._line_window_tile_data_source_claims = {}
        self._line_lcdc_tile_data_source_claims = {}
        self._line_pre_scroll_write_values = None
        self._line_render_x = 0
        self._line_render_complete = False
        self._line_palette_phase_offset = 0
        self._line_mode3_start_scroll_write_offset = 0
        self._line_window_used = False
        self._line_window_active_at_render_x = False
        self._line_window_activation_count = 0
        self._line_window_start_glitch_x = None
        self._line_window_reactivation_glitch_x = None
        self._line_window_enable_early_pulse = False
        self._line_window_enable_cancel_glitch_x = None
        self._line_window_penalty_dots = 0
        self._line_obj_penalty_dots = 0
        self._line_forced_obj_penalty_events = []
        self._line_selected_sprites = None
        self._line_sprite_selection_lcdc = None
        self._line_oam_dma_seen = False
        self._line_oam_dma_hidden_x = None
        self._hblank_stat_interrupt_dot = None

    def _check_window_y_trigger(self) -> None:
        if (
            self.lcd_enabled
            and self._scanline < VISIBLE_LINES
            and self.bus.io[0x4A] == self._scanline
        ):
            self._window_y_triggered = True

    @staticmethod
    def _map_dmg_palette(palette: int, color_id: int) -> int:
        return (palette >> (color_id * 2)) & 0x03

    def _clear_framebuffer(self) -> None:
        for y in range(SCREEN_HEIGHT):
            self.framebuffer[y] = [0 for _ in range(SCREEN_WIDTH)]

    def _set_ly(self, value: int, *, update_lyc: bool = True) -> None:
        self.bus.io[0x44] = value & 0xFF
        if update_lyc:
            self._update_lyc_flag()

    def _preload_next_ly(self, value: int) -> None:
        self.bus.io[0x44] = value & 0xFF
        if self.bus.io[0x44] != self.bus.io[0x45] and self.bus.io[0x41] & 0x04:
            self.bus.io[0x41] = 0x80 | (self.bus.io[0x41] & ~0x04)
            self._update_stat_interrupt_line()

    def _set_mode(self, mode: int, request_interrupt: bool = True) -> None:
        self.bus.io[0x41] = 0x80 | (self.bus.io[0x41] & 0x7C) | (mode & 0x03)
        if request_interrupt:
            self._update_stat_interrupt_line()

    def _update_lyc_flag(self, request_interrupt: bool = True) -> None:
        if self.bus.io[0x44] == self.bus.io[0x45]:
            self.bus.io[0x41] = 0x80 | self.bus.io[0x41] | 0x04
        else:
            self.bus.io[0x41] = 0x80 | (self.bus.io[0x41] & ~0x04)
        if request_interrupt:
            self._update_stat_interrupt_line()

    def _update_stat_interrupt_line(self) -> None:
        stat = self.bus.io[0x41]
        mode = stat & 0x03
        active = False
        active = active or (mode == MODE_HBLANK and bool(stat & 0x08))
        active = active or (mode == MODE_VBLANK and bool(stat & 0x10))
        active = active or (mode == MODE_OAM and bool(stat & 0x20))
        active = active or (bool(stat & 0x04) and bool(stat & 0x40))
        if not self.lcd_enabled:
            self._stat_line = active
            return
        if active and not self._stat_line:
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x02
        self._stat_line = active

    def _request_vblank_oam_stat_interrupt(self) -> None:
        if self.lcd_enabled and self.bus.io[0x41] & 0x20 and not self._stat_line:
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x02

    def _schedule_hblank_stat_interrupt(self) -> None:
        self._hblank_stat_interrupt_dot = self.line_dots

    def _maybe_request_pending_hblank_stat_interrupt(self) -> None:
        if self._hblank_stat_interrupt_dot is None:
            return
        if self.line_dots < self._hblank_stat_interrupt_dot:
            return
        self._hblank_stat_interrupt_dot = None
        if (
            self.lcd_enabled
            and self.mode == MODE_HBLANK
            and self.bus.io[0x41] & 0x08
            and not self._stat_line
        ):
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x02
            self._stat_line = True

    def _maybe_request_spurious_stat_interrupt(self) -> None:
        if not self.lcd_enabled or self._stat_line:
            return
        stat = self.bus.io[0x41]
        mode = stat & 0x03
        if mode in {MODE_HBLANK, MODE_VBLANK, MODE_OAM} or bool(stat & 0x04):
            self.bus.interrupt_flags = self.bus.interrupt_flags | 0x02
