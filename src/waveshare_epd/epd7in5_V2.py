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

    def display_Partial(self, Image, Xstart, Ystart, Xend, Yend):
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

        # Partial mode (0x91) drives the 0x13 RAM with the *opposite* polarity to
        # a full display() refresh on this panel, so the patch must be inverted
        # here. Without this, the partially-updated region renders inverted
        # (white text on a black box) until the next full refresh repaints it.
        inverted = bytes(b ^ 0xFF for b in Image)
        self.send_command(0x13)
        self.send_data2(inverted)

        self.send_command(0x12)
        self.ReadBusy()

    def display_Partial_multi(self, patches):
        """Write multiple partial regions to RAM then trigger a single panel refresh.

        patches: iterable of (image_bytes, Xstart, Ystart, Xend, Yend)

        All regions are written to the controller RAM before the 0x12 refresh
        command is issued, so the panel settles once regardless of band count.
        """
        self.send_command(0x50)
        self.send_data(0xA9)
        self.send_data(0x07)

        for image, Xstart, Ystart, Xend, Yend in patches:
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

            inverted = bytes(b ^ 0xFF for b in image)
            self.send_command(0x13)
            self.send_data2(inverted)

        # Single refresh trigger — panel settles all written regions at once.
        self.send_command(0x12)
        self.ReadBusy()

    def sleep(self):
        self.send_command(0x02) # POWER_OFF
        self.ReadBusy()
        
        self.send_command(0x07) # DEEP_SLEEP
        self.send_data(0XA5)
        
        epdconfig.delay_ms(2000)
        epdconfig.module_exit()
### END OF FILE ###
