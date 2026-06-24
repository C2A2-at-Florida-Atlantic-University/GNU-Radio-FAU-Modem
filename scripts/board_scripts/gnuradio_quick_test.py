import time
from gnuradio import gr
from gnuradio import blocks

class SoftwareSanityCheck(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "Pure Software Verification Flowgraph")
        
        # Instantiate lightweight software-only blocks (Complex float32)
        # Bypasses hardware drivers entirely to isolate the core engine
        src_data = [1.0+1.0j, 2.0+2.0j, 3.0+3.0j, 4.0+4.0j] * 5000
        self.src = blocks.vector_source_c(src_data, repeat=False)
        self.throttle = blocks.throttle(gr.sizeof_gr_complex, 20000, True)
        self.sink = blocks.vector_sink_c()
        
        # Wire block signatures
        self.connect(self.src, self.throttle, self.sink)

if __name__ == '__main__':
    print("[RUNNING] Initializing pure software flowgraph...")
    tb = SoftwareSanityCheck()
    
    print("[RUNNING] Spawning GNU Radio block scheduler threads...")
    tb.start()
    
    print("[RUNNING] Executing sample stream...")
    tb.wait()
    
    output_data = tb.sink.data()
    print(f"[SUCCESS] Flowgraph exited cleanly! Processed {len(output_data)} complex elements safely.")