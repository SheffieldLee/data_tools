import numpy as np
import threading
import multiprocessing
import queue
import h5py
import bcolz


class data_flow(object):
    """
    Given a list of array-like objects, data from the objects is read and
    processed in a parallel thread. All objects are iterated in tandem.
    
    To do preprocessing of data, subclass this class and override the
    _process_batch function.
    
    NOTE: if nb_workers > 1, data loading order is not preserved
    """
    
    def __init__(self, data, batch_size, nb_workers=1,
                 shuffle=False, loop_forever=True):
        self.data = data
        self.batch_size = batch_size
        self.nb_workers = nb_workers
        self.shuffle = shuffle
        self.loop_forever = loop_forever
        
        for d in self.data:
            assert(len(d)==len(data[0]))
                   
        self.num_batches = len(data[0])//self.batch_size
        if len(data[0])%self.batch_size > 0:
            self.num_batches += 1
        
    ''' Generate batches of processed data (output with labels) '''
    def flow(self):
        # Create a stop event to trigger on exceptions/interrupt/termination.
        stop = multiprocessing.Event()
        stop_on_empty = multiprocessing.Event()
        
        # Prepare to start processes + thread.
        load_queue = None
        proc_queue = None
        process_list = []
        preload_thread = None
        try:
            # Create the queues.
            #   NOTE: these can become corrupt on sub-process termination,
            #   so create them in flow() and let them die with the flow().
            load_queue = multiprocessing.JoinableQueue(self.nb_workers)
            proc_queue = multiprocessing.JoinableQueue(self.nb_workers)
            
            # Start the parallel data processing proccess(es)
            for i in range(self.nb_workers):
                process_thread = multiprocessing.Process( \
                    target=self._process_subroutine, 
                    args=(load_queue, proc_queue, stop, stop_on_empty) )
                process_thread.daemon = True
                process_thread.start()
                process_list.append(process_thread)
                
            # Start the parallel loader thread.
            # (must be started AFTER processes to avoid copying it in fork())
            preload_thread = threading.Thread( \
                target=self._preload_subroutine, args=(load_queue, proc_queue,
                                                       stop, stop_on_empty))
            preload_thread.daemon = True
            preload_thread.start()
            
            # Yield batches fetched from the parallel process(es).
            while not stop.is_set():
                try:
                    batch = proc_queue.get()
                    if batch is not None:
                        yield batch
                    proc_queue.task_done()
                except:
                    stop.set()
                    raise
        except:
            stop.set()
            raise
        finally:
            # Clean up, whether there was an exception or not.
            #
            # Set termination event, wait for all processes and threads to
            # end and clear and close all the queues.
            #
            # NOTE: all queues are emptied before joining processes in case the
            # processes block on put().
            stop.set()
            if proc_queue is not None:
                if not proc_queue.empty():
                    print("Warning: proc_queue is not empty on termination! "
                          "Emptying...")
                    while not proc_queue.empty():
                        proc_queue.get()
                proc_queue.close()
            if load_queue is not None:
                if not load_queue.empty():
                    print("Warning: load_queue is not empty on termination! "
                          "Emptying...")
                    while not load_queue.empty():
                        load_queue.get()
                load_queue.close()
            for process in process_list:
                if process.is_alive():
                    process.join()
            if preload_thread is not None:
                preload_thread.join()
    
    ''' Do preprocessing on the batch here.
        Subclass the class to customize this function.
        
        NOTE that a batch should not be None as this is used internally as a
        signal to stop getting from proc_queue. '''
    def _process_batch(self, batch):
        return batch
    
    ''' Preload batches in the background and add them into the load_queue.
        Wait if the queue is full. '''
    def _preload_subroutine(self, load_queue, proc_queue,
                            stop, stop_on_empty):
        while not stop.is_set():
            if self.shuffle:
                indices = np.random.permutation(len(self))
            else:
                indices = np.arange(len(self))
                
            for b in range(self.num_batches):
                try:
                    if not stop.is_set():
                        bs = self.batch_size
                        batch_indices = indices[b*bs:(b+1)*bs]
                        batch = []
                        for d in self.data:
                            batch.append([d[i][...] for i in batch_indices])
                        load_queue.put( batch )
                except:
                    stop.set()
                    raise
                
            # Wait for all queued items to be processed and yielded, then exit.
            if not self.loop_forever:
                stop_on_empty.set()
                load_queue.join()     # Wait for processing
                proc_queue.join()     # Wait for yielding
                proc_queue.put(None)  # Stop blocking on get() in main thread
                proc_queue.join()     # Wait for main thread to get None
                stop.set()
                
    ''' Process any loaded batches in the load queue and add them to the
        processed queue -- these are ready to yield. '''
    def _process_subroutine(self, load_queue, proc_queue,
                            stop, stop_on_empty):
        while not stop.is_set():
            try:
                batch = load_queue.get(block=not stop_on_empty.is_set())
                proc_queue.put(self._process_batch(batch))
                load_queue.task_done()
            except queue.Empty:
                # stop_on_empty is set; wait for all batches to be yielded.
                #
                # Wait for load_queue in case some processes have done get()
                # and the queue is empty but task_done has not yet been called.
                #
                # Wait on proc_queue _after_ load_queue because all processes
                # will have already put() into proc_queue after load_queue
                # unblocks (they call task_done after put).
                load_queue.join()
                proc_queue.join()
                stop.set()
            except:
                stop.set()
                raise
            
    def __len__(self):
        return len(self.data[0])



class buffered_array_writer(object):
    """
    Given an array, data element shape, and batch size, writes data to an array
    batch-wise. Data can be passed in any number of elements at a time.
    If the array is an interface to a memory-mapped file, data is thus written
    batch-wise to the file.
    
    INPUTS
    storage_array      : the array to write into
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    length             : dataset length (if None, expand it dynamically)
    """
    def __init__(self, storage_array, data_element_shape, dtype, batch_size,
                 length=None):
        self.storage_array = storage_array
        self.data_element_shape = data_element_shape
        self.dtype = dtype
        self.batch_size = batch_size
        self.length = length
        
        self.buffer = np.zeros((batch_size,)+data_element_shape, dtype=dtype)
        self.buffer_ptr = 0
        self.storage_array_ptr = 0
        
    ''' Flush the buffer. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            end = self.storage_array_ptr+self.buffer_ptr
            self.storage_array[self.storage_array_ptr:end] = self.buffer[:self.buffer_ptr]
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0
            
    '''
    Write data to file one buffer-full at a time. Note: data is not written
    until buffer is full.
    '''
    def buffered_write(self, data):
        # Verify data shape
        if data.shape != self.data_element_shape \
                                 and data.shape[1:] != self.data_element_shape:
            raise ValueError("Error: input data has the wrong shape.")
        if data.shape == self.data_element_shape:
            data_len = 1
        elif data[1:].shape == self.data_element_shape:
            data_len = len(data)
            
        # Stop when data length exceeded
        if self.length is not None and self.length==self.storage_array_ptr:
            raise EOFError("Write aborted: length of input data exceeds "
                           "remaining space.")
            
        # Verify data type
        if data.dtype != self.dtype:
            raise TypeError
            
        # Buffer/write
        if data_len == 1:
            data = [data]
        for d in data:
            self.buffer[self.buffer_ptr] = d
            self.buffer_ptr += 1
            
            # Flush buffer when full
            if self.buffer_ptr==self.batch_size:
                self.flush_buffer()
                
        # Flush the buffer when 'length' reached
        if self.length is not None \
                       and self.storage_array_ptr+self.buffer_ptr==self.length:
            self.flush_buffer()
            
    def __len__(self):
        num_elements = len(self.storage_array)+self.buffer_ptr
        return num_elements
            
    def get_shape(self):
        return (len(self),)+self.data_element_shape
    
    def get_element_shape(self):
        return self.data_element_shape
    
    def get_array(self):
        return self.storage_array
        
    def __del__(self):
        self.flush_buffer()


class h5py_array_writer(buffered_array_writer):
    """
    Given a data element shape and batch size, writes data to an HDF5 file
    batch-wise. Data can be passed in any number of elements at a time.
    
    INPUTS
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    filename           : name of file in which to store data
    array_name         : HDF5 array path
    length             : dataset length (if None, expand it dynamically)
    append             : write files with append mode instead of write mode
    kwargs             : dictionary of arguments to pass to h5py on dataset creation
                         (if none, do lzf compression with batch_size chunk
                         size)
    """
    
    def __init__(self, data_element_shape, dtype, batch_size, filename,
                 array_name, length=None, append=False, kwargs={}):
        super(h5py_array_writer, self).__init__(None, data_element_shape,
                                                dtype, batch_size, length)
        self.filename = filename
        self.array_name = array_name
        self.kwargs = kwargs
        if self.kwargs=={}:
            self.kwargs = {'chunks': (batch_size,)+data_element_shape,
                           'compression': 'lzf'}
    
        # Open the file for writing.
        self.file = None
        if append:
            self.write_mode = 'a'
        else:
            self.write_mode = 'w'
        try:
            self.file = h5py.File(filename, self.write_mode)
        except:
            print("Error: failed to open file %s" % filename)
            raise
        
        # Open an array interface (check if the array exists; if not, create it)
        if self.length is None:
            ds_args = (self.array_name, (1,)+self.data_element_shape)
        else:
            ds_args = (self.array_name, (self.length,)+self.data_element_shape)
        try:
            self.storage_array = self.file[self.array_name]
            self.storage_array_ptr = len(self.storage_array)
        except KeyError:
            self.storage_array = self.file.create_dataset( *ds_args,
                               dtype=self.dtype,
                               maxshape=(self.length,)+self.data_element_shape,
                               **self.kwargs )
            self.storage_array_ptr = 0
            
    ''' Flush the buffer. Resize the dataset, if needed. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            end = self.storage_array_ptr+self.buffer_ptr
            if self.length is None:
                self.storage_array.resize( (end,)+self.data_element_shape )
            self.storage_array[self.storage_array_ptr:end] = \
                                                  self.buffer[:self.buffer_ptr]
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0
    
    ''' Flush remaining data in the buffer to file and close the file. '''
    def __del__(self):
        self.flush_buffer()
        if self.file is not None:
            self.file.close() 


class bcolz_array_writer(buffered_array_writer):
    """
    Given a data element shape and batch size, writes data to an bcolz file-set batch-wise. Data can be passed in any number of elements at a time.
    
    INPUTS
    data_element_shape : shape of one input element
    batch_size         : write the data to disk in batches of this size
    save_path          : directory to save array in
    length             : dataset length (if None, expand it dynamically)
    append             : write files with append mode instead of write mode
    kwargs             : dictionary of arguments to pass to bcolz on dataset creation
                         (if none, do blosc compression with chunklen determined by the expected array length)
    """
    
    def __init__(self, data_element_shape, dtype, batch_size, save_path, length=None, append=False, kwargs={}):
        super(bcolz_array_writer, self).__init__(None, data_element_shape, dtype, batch_size, length)
        self.save_path = save_pathsavepath
        self.kwargs = kwargs
        if self.kwargs=={}:
            self.kwargs = {'expectedlen': length, 'cparams': bcolz.cparams(clevel=5, shuffle=True, cname='blosclz')}
    
        # Create the file-backed array, open for writing.
        if append:
            self.write_mode = 'a'
        else:
            self.write_mode = 'w'
        try:
            self.storage_array = bcolz.zeros( shape=(0,)+data_element_shape, dtype=np.float32, rootdir=self.save_path, mode=self.write_mode,
                                              **self.kwargs )
        except:
            print("Error: failed to create file-backed bcolz storage array.")
            raise
            
    ''' Flush the buffer. '''
    def flush_buffer(self):
        if self.buffer_ptr > 0:
            self.storage_array.append(self.buffer[:self.buffer_ptr])
            self.storage_array.flush()
            self.storage_array_ptr += self.buffer_ptr
            self.buffer_ptr = 0