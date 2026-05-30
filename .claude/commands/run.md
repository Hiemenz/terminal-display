# Run terminal-display

Run the terminal-display app and confirm the output image looks correct.

## What this skill does

1. Installs dependencies if needed (`poetry install`)
2. Runs `main.py --once --local` to render one frame
3. Opens / reports `output/terminal.bmp` so you can see the result
4. Reports any errors and suggests fixes

## Steps

1. Check we're in the right directory (`/Users/kevinhiemenz/git/terminal-display`).
   If not, cd there or report an error.

2. Run `poetry install` to ensure psutil and pillow are installed.

3. Run the pipeline:
   ```
   poetry run python main.py --once --local
   ```

4. If it succeeded, open the output image with:
   ```
   open output/terminal.bmp
   ```
   and report what was generated (stats shown, any missing panels).

5. If it failed, read the traceback, identify the root cause, and fix it before
   reporting back to the user.

## Common issues

- **psutil not found** → run `poetry install`
- **Font not found** → falls back to PIL default automatically; no action needed
- **waveshare_epd import error on macOS** → expected; display_eink.py skips hardware
- **Permission errors on disk/network** → psutil may need sudo on some Linux configs;
  not needed on macOS

## Iterating on the layout

When the user wants to change the layout or add panels, edit `src/render.py` and
re-run this skill to see the result. The render function signature is:
```python
render(stats: dict, config: dict) -> PIL.Image
```
