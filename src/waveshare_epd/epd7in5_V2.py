# *****************************************************************************
# * | File        :	  epd7in5.py
# * | Author      :   Waveshare team
# * | Function    :   Electronic paper driver
# * | Info        :
# *----------------
# * | This version:   V4.0
# * | Date        :   2019-06-20
# # | Info        :   python demo
# -----------------------------------------------------------------------------
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documnetation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to  whom the Software is
# furished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS OR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#


import logging
import time
from . import epdconfig

# Display resolution
EPD_WIDTH       = 800
EPD_HEIGHT      = 480

logger = logging.getLogger(__name__)

class EPD:
    def __init__(self):
        self.reset_pin = epdconfig.RST_PIN
        self.dc_pin = epdconfig.DC_PIN
        self.busy_pin = epdconfig.BUSY_PIN
        self.cs_pin = epdconfig.CS_PIN
        self.width = EPD_WIDTH
        self.height = EPD_HEIGHT

    # ── Flicker-free direct-update (DU / "A2") partial refresh ────────────────
    # The stock OTP partial waveform physically cycles every pixel in the
    # addressed window black→white→black, which reads as a flash where text
    # changes. Loading a custom register LUT lets us define a *direct-update*
    # waveform where unchanged pixels (W→W, B→B) get ZERO voltage and only
    # changed pixels are driven straight to their target — so a character just
    # appears, with no flash. This is the standard "A2"/DU technique applied to
    # the UC8179 controller in the 7.5" V2 panel.
    #
    # Voltage frame (known-good values for this panel, from the register-LUT
    # variant of the Waveshare driver):
    #   [OSC, VSH, VSL, VSHR, VCOM, (unused), VGHL]
    _VOLTAGE_FRAME_DU = [0x06, 0x3F, 0x3F, 0x11, 0x24, 0x07, 0x17]

    # Frames the drive phase runs for. The single biggest tuning knob: too few
    # and characters come out faint/grey or ghost; too many and the update slows
    # and the transition becomes faintly visible. ~20 is a sane starting point.
    _DU_FRAMES = 0x14

    def _du_luts(self, frames=None):
        """Build the five 42-byte (7 phases × 6 bytes) DU LUTs.

        Phase byte layout: [VS, F1, F2, F3, F4, RP].
          VS packs four 2-bit sub-frame voltage levels (bits 7:6,5:4,3:2,1:0):
          00=GND(hold), 01=VDH, 10=VDL, 11=VDHR.  F1..F4 = sub-frame frame
          counts.  RP = phase repeat count (0 ⇒ phase inactive/skipped).

        On this panel VDH drives a pixel white and VDL drives it black. So:
          WW / BB → all zero  → no voltage → pixel holds (no flash).
          BW (black→white)    → one VDH phase.
          WB (white→black)    → one VDL phase.
        """
        n = self._DU_FRAMES if frames is None else int(frames)
        pad = [0x00] * 36
        vcom = [0x00, n, 0x00, 0x00, 0x00, 0x01] + pad   # hold VCOM-DC during drive
        ww   = [0x00] * 42                                # white stays white: no move
        bb   = [0x00] * 42                                # black stays black: no move
        bw   = [0x40, n, 0x00, 0x00, 0x00, 0x01] + pad   # black→white: VDH
        wb   = [0x80, n, 0x00, 0x00, 0x00, 0x01] + pad   # white→black: VDL
        return vcom, ww, bw, wb, bb

    def SetLut(self, lut_vcom, lut_ww, lut_bw, lut_wb, lut_bb):
        """Load the five waveform LUTs (VCOM, WW, BW, WB, BB) into registers."""
        self.send_command(0x20); self.send_data2(bytes(lut_vcom))
        self.send_command(0x21); self.send_data2(bytes(lut_ww))
        self.send_command(0x22); self.send_data2(bytes(lut_bw))
        self.send_command(0x23); self.send_data2(bytes(lut_wb))
        self.send_command(0x24); self.send_data2(bytes(lut_bb))

    def init_du(self, frames=None):
        """Init the panel in register-LUT mode with the flash-free DU waveform.

        `frames` overrides the drive-phase frame count (_DU_FRAMES) — more frames
        fully transition heavy/inverse content, fewer keep light text crisp+fast.

        Mirrors the panel's full-init power/voltage setup, but sets the panel to
        read its waveform from registers (PSR=0x3F) and loads the DU LUTs. Call
        once before issuing display_du() updates; re-init after any OTP refresh
        (full flash) since that leaves the panel in OTP-LUT mode."""
        if epdconfig.module_init() != 0:
            return -1
        self.reset()
        vf = self._VOLTAGE_FRAME_DU

        self.send_command(0x01)        # POWER SETTING
        self.send_data(0x17)           # internal DC/DC
        self.send_data(vf[6])          # VGH/VGL
        self.send_data(vf[1])          # VSH
        self.send_data(vf[2])          # VSL
        self.send_data(vf[3])          # VSHR

        self.send_command(0x82)        # VCOM DC
        self.send_data(vf[4])

        self.send_command(0x06)        # Booster soft start
        self.send_data(0x27)
        self.send_data(0x27)
        self.send_data(0x2F)
        self.send_data(0x17)

        self.send_command(0x30)        # OSC (frame rate)
        self.send_data(vf[0])

        self.send_command(0x04)        # POWER ON
        epdconfig.delay_ms(100)
        self.ReadBusy()

        self.send_command(0x00)        # PANEL SETTING — 0x3F: LUT from registers
        self.send_data(0x3F)

        self.send_command(0x61)        # resolution 800×480
        self.send_data(0x03)
        self.send_data(0x20)
        self.send_data(0x01)
        self.send_data(0xE0)

        self.send_command(0x15)
        self.send_data(0x00)

        self.send_command(0x50)        # VCOM and data interval
        self.send_data(0x10)
        self.send_data(0x07)

        self.send_command(0x60)        # TCON
        self.send_data(0x22)

        self.send_command(0x65)        # resolution
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)
        self.send_data(0x00)

        self.SetLut(*self._du_luts(frames))
        return 0

    def display_du(self, image, old_image=None):
        """Flash-free full-frame refresh using the loaded DU LUTs.

        image/old_image are getbuffer() output (bit=1 → black). The controller
        selects a per-pixel waveform from (old, new): unchanged pixels hit the
        all-zero WW/BB LUTs and get no voltage, so only changed pixels move — the
        whole screen is rewritten but nothing flashes. Supplying old_image keeps
        the controller's reference frame correct even right after switching into
        DU mode.

        Register-LUT mode (PSR=0x3F / 0x91) reads the 0x10/0x13 RAM with the
        *opposite* polarity to a full display() refresh — the same regime as
        display_Partial(), which inverts for exactly this reason. Without the
        inversion the controller reads a new black pixel as "white", picks the
        BW (→VDH→white) waveform and paints the glyph white-on-white, so typed
        characters come out invisible. Invert both buffers so image-black maps to
        the WB (→VDL→black) waveform and characters render."""
        if old_image is not None:
            self.send_command(0x10)
            self.send_data2(bytes(b ^ 0xFF for b in old_image))
        self.send_command(0x13)
        self.send_data2(bytes(b ^ 0xFF for b in image))
        self.send_command(0x12)
        epdconfig.delay_ms(50)
        self.ReadBusy()

    # Hardware reset
    def reset(self):
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20) 
        epdconfig.digital_write(self.reset_pin, 0)
        epdconfig.delay_ms(2)
        epdconfig.digital_write(self.reset_pin, 1)
        epdconfig.delay_ms(20)   

    def send_command(self, command):
        epdconfig.digital_write(self.dc_pin, 0)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte([command])
        epdconfig.digital_write(self.cs_pin, 1)

    def send_data(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.spi_writebyte([data])
        epdconfig.digital_write(self.cs_pin, 1)

    def send_data2(self, data):
        epdconfig.digital_write(self.dc_pin, 1)
        epdconfig.digital_write(self.cs_pin, 0)
        epdconfig.SPI.writebytes2(data)
        epdconfig.digital_write(self.cs_pin, 1)

    def ReadBusy(self):
        logger.debug("e-Paper busy")
        self.send_command(0x71)
        busy = epdconfig.digital_read(self.busy_pin)
        deadline = time.monotonic() + 15.0
        while busy == 0:
            if time.monotonic() > deadline:
                logger.error('e-Paper ReadBusy timeout — panel is stuck, forcing reinit')
                raise IOError('e-Paper BUSY pin timeout (15s)')
            epdconfig.delay_ms(10)
            self.send_command(0x71)
            busy = epdconfig.digital_read(self.busy_pin)
        epdconfig.delay_ms(10)
        logger.debug("e-Paper busy release")
        
    def init(self):
        if (epdconfig.module_init() != 0):
            return -1
        # EPD hardware init start
        self.reset()
        
        self.send_command(0x06)     # btst
        self.send_data(0x17)
        self.send_data(0x17)
        self.send_data(0x28)        # If an exception is displayed, try using 0x38
        self.send_data(0x17)
        
        self.send_command(0x01)			#POWER SETTING
        self.send_data(0x07)
        self.send_data(0x07)    #VGH=20V,VGL=-20V
        self.send_data(0x3f)		#VDH=15V
        self.send_data(0x3f)		#VDL=-15V

        self.send_command(0x04) #POWER ON
        epdconfig.delay_ms(100)
        self.ReadBusy()

        self.send_command(0X00)			#PANNEL SETTING
        self.send_data(0x1F)   #KW-3f   KWR-2F	BWROTP 0f	BWOTP 1f

        self.send_command(0x61)        	#tres
        self.send_data(0x03)		#source 800
        self.send_data(0x20)
        self.send_data(0x01)		#gate 480
        self.send_data(0xE0)

        self.send_command(0X15)
        self.send_data(0x00)

        self.send_command(0X50)			#VCOM AND DATA INTERVAL SETTING
        self.send_data(0x10)
        self.send_data(0x07)

        self.send_command(0X60)			#TCON SETTING
        self.send_data(0x22)

        # EPD hardware init end
        return 0
    
    def init_fast(self):
        if (epdconfig.module_init() != 0):
            return -1
        # EPD hardware init start
        self.reset()
        
        self.send_command(0X00)			#PANNEL SETTING
        self.send_data(0x1F)   #KW-3f   KWR-2F	BWROTP 0f	BWOTP 1f

        self.send_command(0X50)			#VCOM AND DATA INTERVAL SETTING
        self.send_data(0x10)
        self.send_data(0x07)

        self.send_command(0x04) #POWER ON
        epdconfig.delay_ms(100) 
        self.ReadBusy()        #waiting for the electronic paper IC to release the idle signal

        #Enhanced display drive(Add 0x06 command)
        self.send_command(0x06)			#Booster Soft Start 
        self.send_data (0x27)
        self.send_data (0x27)   
        self.send_data (0x18)		
        self.send_data (0x17)		

        self.send_command(0xE0)
        self.send_data(0x02)
        self.send_command(0xE5)
        self.send_data(0x5A)

        # EPD hardware init end
        return 0
    
    def init_part(self):
        if (epdconfig.module_init() != 0):
            return -1
        # EPD hardware init start
        self.reset()

        self.send_command(0X00)			#PANNEL SETTING
        self.send_data(0x1F)   #KW-3f   KWR-2F	BWROTP 0f	BWOTP 1f

        self.send_command(0x04) #POWER ON
        epdconfig.delay_ms(100) 
        self.ReadBusy()        #waiting for the electronic paper IC to release the idle signal

        self.send_command(0xE0)
        self.send_data(0x02)
        self.send_command(0xE5)
        self.send_data(0x6E)

        # EPD hardware init end
        return 0

    def getbuffer(self, image):
        img = image
        imwidth, imheight = img.size
        if(imwidth == self.width and imheight == self.height):
            img = img.convert('1')
        elif(imwidth == self.height and imheight == self.width):
            # image has correct dimensions, but needs to be rotated
            img = img.rotate(90, expand=True).convert('1')
        else:
            logger.warning("Wrong image dimensions: must be " + str(self.width) + "x" + str(self.height))
            # return a blank buffer
            return [0x00] * (int(self.width/8) * self.height)

        return bytearray(b ^ 0xFF for b in img.tobytes('raw'))

    def getbuffer_partial(self, image):
        """Convert an arbitrarily-sized PIL image to e-paper format bytes for display_Partial."""
        return bytearray(b ^ 0xFF for b in image.convert('1').tobytes('raw'))

    def display(self, image):
        image1 = bytearray(b ^ 0xFF for b in image)
        self.send_command(0x10)
        self.send_data2(image1)

        self.send_command(0x13)
        self.send_data2(image)

        self.send_command(0x12)
        self.ReadBusy()

    def Clear(self):
        self.send_command(0x10)
        self.send_data2([0xFF] * int(self.width * self.height / 8))
        self.send_command(0x13)
        self.send_data2([0x00] * int(self.width * self.height / 8))

        self.send_command(0x12)
        epdconfig.delay_ms(100)
        self.ReadBusy()

    def display_Partial(self, Image, Xstart, Ystart, Xend, Yend, old_image=None):
        if((Xstart % 8 + Xend % 8 == 8 & Xstart % 8 > Xend % 8) | Xstart % 8 + Xend % 8 == 0 | (Xend - Xstart)%8 == 0):
            Xstart = Xstart // 8 * 8
            Xend = Xend // 8 * 8
        else:
            Xstart = Xstart // 8 * 8
            if Xend % 8 == 0:
                Xend = Xend // 8 * 8
            else:
                Xend = Xend // 8 * 8 + 1

        self.send_command(0x50)
        self.send_data(0xA9)
        self.send_data(0x07)

        self.send_command(0x91)		#This command makes the display enter partial mode
        self.send_command(0x90)		#resolution setting
        self.send_data (Xstart//256)
        self.send_data (Xstart%256)   #x-start

        self.send_data ((Xend-1)//256)
        self.send_data ((Xend-1)%256)  #x-end

        self.send_data (Ystart//256)  #
        self.send_data (Ystart%256)   #y-start

        self.send_data ((Yend-1)//256)
        self.send_data ((Yend-1)%256)  #y-end
        self.send_data (0x01)

        # The panel computes a partial update as the *difference* between the
        # 0x10 RAM (what's currently on screen) and the 0x13 RAM (the new image).
        # The 0x10 buffer is only set by display()/full refresh; after the panel
        # sleeps, that RAM goes stale/corrupt and the next partial renders as
        # noise. When the caller knows the prior on-screen content (old_image),
        # re-prime 0x10 with it so the diff is computed correctly.
        # See: https://hchargois.github.io/ (betterepd7in5)
        if old_image is not None:
            self.send_command(0x10)
            self.send_data2(bytes(b ^ 0xFF for b in old_image))

        # Partial mode (0x91) drives the 0x13 RAM with the *opposite* polarity to
        # a full display() refresh on this panel, so the patch must be inverted
        # here. Without this, the partially-updated region renders inverted
        # (white text on a black box) until the next full refresh repaints it.
        inverted = bytes(b ^ 0xFF for b in Image)
        self.send_command(0x13)
        self.send_data2(inverted)

        self.send_command(0x12)
        epdconfig.delay_ms(50)   # allow panel to assert BUSY before polling
        self.ReadBusy()

    def display_Partial_multi(self, patches):
        """Write multiple partial regions to RAM then trigger a single panel refresh.

        patches: iterable of (image_bytes, old_bytes_or_None, Xstart, Ystart, Xend, Yend)

        When old_bytes is provided, it is the prior on-screen content for that
        window; it re-primes the 0x10 back-buffer so the panel computes a clean
        partial diff even after the display has slept (otherwise the 0x10 RAM is
        stale and the region renders as noise). See display_Partial() for detail.

        All regions are written to the controller RAM before the 0x12 refresh
        command is issued, so the panel settles once regardless of band count.
        """
        self.send_command(0x50)
        self.send_data(0xA9)
        self.send_data(0x07)

        for image, old_image, Xstart, Ystart, Xend, Yend in patches:
            # Same x byte-alignment as display_Partial.
            if((Xstart % 8 + Xend % 8 == 8 & Xstart % 8 > Xend % 8) | Xstart % 8 + Xend % 8 == 0 | (Xend - Xstart) % 8 == 0):
                Xstart = Xstart // 8 * 8
                Xend = Xend // 8 * 8
            else:
                Xstart = Xstart // 8 * 8
                Xend = Xend // 8 * 8 if Xend % 8 == 0 else Xend // 8 * 8 + 1

            self.send_command(0x91)       # enter partial mode for this window
            self.send_command(0x90)       # set partial window
            self.send_data(Xstart // 256)
            self.send_data(Xstart % 256)
            self.send_data((Xend - 1) // 256)
            self.send_data((Xend - 1) % 256)
            self.send_data(Ystart // 256)
            self.send_data(Ystart % 256)
            self.send_data((Yend - 1) // 256)
            self.send_data((Yend - 1) % 256)
            self.send_data(0x01)

            if old_image is not None:
                self.send_command(0x10)
                self.send_data2(bytes(b ^ 0xFF for b in old_image))

            inverted = bytes(b ^ 0xFF for b in image)
            self.send_command(0x13)
            self.send_data2(inverted)

        # Single refresh trigger — panel settles all written regions at once.
        self.send_command(0x12)
        epdconfig.delay_ms(50)   # allow panel to assert BUSY before polling
        self.ReadBusy()

    def sleep(self):
        self.send_command(0x02) # POWER_OFF
        self.ReadBusy()
        
        self.send_command(0x07) # DEEP_SLEEP
        self.send_data(0XA5)
        
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()
### END OF FILE ###
