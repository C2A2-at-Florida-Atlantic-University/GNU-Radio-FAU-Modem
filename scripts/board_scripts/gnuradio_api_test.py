import numpy as np
from gnuradio import gr
from gnuradio import blocks
import gnuradio.gr_unittest as gr_unittest

class OfficialApiSanity(gr_unittest.TestCase):
    def setUp(self):
        self.tb = gr.top_block()

    def test_001_variable_type_integrity(self):
        src_data = (1.0, 2.0, 3.0, 4.0)
        src = blocks.vector_source_f(src_data, False)
        sink = blocks.vector_sink_f()
        self.tb.connect(src, sink)
        self.tb.run()
        
        # Using the official GNU Radio floating-point tuple assertion macro
        self.assertFloatTuplesAlmostEqual(src_data, sink.data(), places=5)

if __name__ == '__main__':
    gr_unittest.main()