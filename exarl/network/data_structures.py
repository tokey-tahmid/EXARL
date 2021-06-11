# © (or copyright) 2020. Triad National Security, LLC. All rights reserved.
#
# This program was produced under U.S. Government contract 89233218CNA000001 for Los Alamos
# National Laboratory (LANL), which is operated by Triad National Security, LLC for the U.S.
# Department of Energy/National Nuclear Security Administration. All rights in the program are
# reserved by Triad National Security, LLC, and the U.S. Department of Energy/National Nuclear
# Security Administration. The Government is granted for itself and others acting on its behalf a
# nonexclusive, paid-up, irrevocable worldwide license in this material to reproduce, prepare
# derivative works, distribute copies to the public, perform publicly and display publicly, and
# to permit others to do so.

import sys
import os
import numpy as np
from exarl.base import ExaData
from exarl.base.comm_base import ExaComm
from exarl.network.simple_comm import ExaSimple
MPI=ExaSimple.MPI

class ExaMPIDistributedQueue(ExaData):
    def __init__(self, comm, rank=None, size=None, data=None, length=32, max_model_lag=None, failPush=False):
        self.comm = comm
        self.length = length
        # This lets us fail a push when at full capacity
        # Otherwise will overwrite the oldest data
        self.failPush = failPush

        if data is not None:
            dataBytes = MPI.pickle.dumps(data)
            size = len(dataBytes)
        super().__init__(bytes, size, comm_size=comm.size, max_model_lag=max_model_lag)
        self.buff = bytearray(self.dataSize)
        self.plus = np.array([1], dtype=np.int64)
        self.minus = np.array([-1], dtype=np.int64)

        totalSize = 0
        self.headBuff = None
        self.tailBuff = None
        disp = MPI.DOUBLE.Get_size()
        if ExaComm.is_actor():
            totalSize = size * self.length
            self.headBuff = np.zeros(1, dtype=np.int64)
            self.tailBuff = np.zeros(1, dtype=np.int64)

        # Setup head window
        self.head = MPI.Win.Create(self.headBuff, disp, comm=self.comm.raw())

        # Setup tail window
        self.tail = MPI.Win.Create(self.tailBuff, disp, comm=self.comm.raw())

        # Setup data window
        self.win = MPI.Win.Allocate(totalSize, disp_unit=size, comm=self.comm.raw())

    def __del__(self):
        self.win.Free()

    def pop(self, rank, count=1):
        ret = True
        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head.Lock(rank)
        self.tail.Lock(rank)

        # Read the head and tail pointers.
        reqHead = self.head.Rget_accumulate(self.minus, head, rank, op=MPI.NO_OP)
        reqTail = self.tail.Rget_accumulate(self.plus, tail, rank, op=MPI.SUM)
        reqHead.wait()
        reqTail.wait()

        # Is there space
        if head[0] > tail[0]:
            index = tail[0] % self.length
            self.win.Lock(rank)
            self.win.Get_accumulate(
                self.buff,
                self.buff,
                rank,
                target=[index, self.dataSize],
                op=MPI.NO_OP,
            )
            self.win.Unlock(rank)
        else:
            # Dec the tail pointer
            self.tail.Accumulate(self.minus, rank, op=MPI.SUM)
            ret = False

        self.tail.Unlock(rank)
        self.head.Unlock(rank)

        if ret:
            return MPI.pickle.loads(self.buff)
        return None

    def push(self, data, rank=None):
        if rank is None:
            rank = self.comm.rank
        toSend = MPI.pickle.dumps(data)
        assert len(toSend) <= self.dataSize

        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)

        self.head.Lock(rank)
        self.tail.Lock(rank)
        reqHead = self.head.Rget_accumulate(self.plus, head, rank, op=MPI.SUM)
        reqTail = self.tail.Rget_accumulate(self.plus, tail, rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()

        write = True
        headIndex = head[0] % self.length
        tailIndex = tail[0] % self.length
        if head[0] > tail[0] and headIndex == tailIndex:
            if self.failPush:
                write = False
                self.head.Accumulate(
                    self.minus, rank, op=MPI.SUM
                )
            else:
                self.tail.Accumulate(
                    self.plus, rank, op=MPI.SUM
                )
            lost = 1
            capacity = self.length
        else:
            lost = 0
            capacity = head[0] - tail[0]

        if write:    
            self.win.Lock(rank)
            self.win.Accumulate(
                toSend, rank, target=[headIndex, len(toSend)], op=MPI.REPLACE
            )
            self.win.Unlock(rank)

        self.tail.Unlock(rank)
        self.head.Unlock(rank)
        
        return capacity, lost

class ExaMPIDistributedStack(ExaData):
    def __init__(self, comm, rank=None, size=None, data=None, length=32, max_model_lag=None, failPush=False):
        self.comm = comm
        self.length = length
        # This lets us fail a push when at full capacity
        # Otherwise will overwrite the oldest data
        self.failPush = failPush

        if data is not None:
            dataBytes = MPI.pickle.dumps(data)
            size = len(dataBytes)
        super().__init__(bytes, size, comm_size=comm.size, max_model_lag=max_model_lag)
        self.buff = bytearray(self.dataSize)
        self.plus = np.array([1], dtype=np.int64)
        self.minus = np.array([-1], dtype=np.int64)

        totalSize = 0
        self.headBuff = None
        self.tailBuff = None
        disp = MPI.DOUBLE.Get_size()
        if ExaComm.is_actor():
            totalSize = size * self.length
            self.headBuff = np.zeros(1, dtype=np.int64)
            self.tailBuff = np.zeros(1, dtype=np.int64)

        # Setup head window
        self.head = MPI.Win.Create(self.headBuff, disp, comm=self.comm.raw())

        # Setup tail window
        self.tail = MPI.Win.Create(self.tailBuff, disp, comm=self.comm.raw())

        # Setup data window
        self.win = MPI.Win.Allocate(totalSize, disp_unit=size, comm=self.comm.raw())

    def __del__(self):
        self.win.Free()

    def pop(self, rank, count=1):
        ret = False
        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head.Lock(rank)
        self.tail.Lock(rank)

        # Read the head and tail pointers.
        reqHead = self.head.Rget_accumulate(self.minus, head, rank, op=MPI.SUM)
        reqTail = self.tail.Rget_accumulate(self.minus, tail, rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()
        # print("InPop", head[0], tail[0])
        if head[0] > tail[0]:
            ret = True
            index = (head[0] - 1) % self.length

            self.win.Lock(rank)
            self.win.Get_accumulate(
                self.buff,
                self.buff,
                rank,
                target=[index, self.dataSize],
                op=MPI.NO_OP,
            )
            self.win.Unlock(rank)

        else:
            self.head.Accumulate(
                self.plus, rank, op=MPI.SUM
            )

        self.tail.Unlock(rank)
        self.head.Unlock(rank)

        if ret:
            return MPI.pickle.loads(self.buff)
        return None

    def push(self, data, rank=None):
        if rank is None:
            rank = self.comm.rank
        toSend = MPI.pickle.dumps(data)
        assert len(toSend) == self.dataSize

        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head.Lock(rank)
        self.tail.Lock(rank)

        # Read the head and tail pointers.
        reqHead = self.head.Rget_accumulate(self.plus, head, rank, op=MPI.SUM)
        reqTail = self.tail.Rget_accumulate(self.plus, tail, rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()

        # This is if we are going to loose data because we exceded capacity
        write = True
        if tail[0] + self.length == head[0]:
            if self.failPush:
                write = False
                self.head.Accumulate(
                    self.minus, rank, op=MPI.SUM
                )
            else:
                self.tail.Accumulate(
                    self.plus, rank, op=MPI.SUM
                )
            lost = 1
            capacity = self.length
        else:
            lost = 0
            capacity = head[0] - tail[0] + 1

        if write:
            # Actual write data
            index = head[0] % self.length
            self.win.Lock(rank)
            self.win.Accumulate(
                toSend, rank, target=[index, self.dataSize], op=MPI.REPLACE
            )
            self.win.Unlock(rank)
            
        self.tail.Unlock(rank)
        self.head.Unlock(rank)
        return capacity, lost

class ExaMPICentralizedStack(ExaData):
    def __init__(self, comm, rank=None, size=None, data=None, length=32, max_model_lag=None, failPush=False):
        self.comm = comm
        self.rank = rank
        self.length = length
        # This lets us fail a push when at full capacity
        # Otherwise will overwrite the oldest data
        self.failPush = failPush

        if data is not None:
            dataBytes = MPI.pickle.dumps(data)
            size = len(dataBytes)
        super().__init__(bytes, size, comm_size=comm.size, max_model_lag=max_model_lag)
        self.buff = bytearray(self.dataSize)
        self.plus = np.array([1], dtype=np.int64)
        self.minus = np.array([-1], dtype=np.int64)

        totalSize = 0
        headSize = 0
        tailSize = 0
        if comm.rank == rank:
            totalSize = size * self.length
            headSize = MPI.INT64_T.Get_size()
            tailSize = MPI.INT64_T.Get_size()

        self.head = []
        self.tail = []
        self.win = []
        for i in range(comm.size):
            # Setup head window
            self.head.append(MPI.Win.Allocate(headSize, comm=self.comm.raw()))
            self.head[i].Lock(self.rank)
            self.head[i].Accumulate(
                np.zeros(1, dtype=np.int64), self.rank, op=MPI.REPLACE
            )
            self.head[i].Unlock(self.rank)
            self.head[i].Fence(self.rank)

            # Setup tail window
            self.tail.append(MPI.Win.Allocate(tailSize, comm=self.comm.raw()))
            self.tail[i].Lock(self.rank)
            self.tail[i].Accumulate(
                np.zeros(1, dtype=np.int64), self.rank, op=MPI.REPLACE
            )
            self.tail[i].Unlock(self.rank)
            self.tail[i].Fence(self.rank)

            # Setup data window
            self.win.append(
                MPI.Win.Allocate(totalSize, disp_unit=size, comm=self.comm.raw())
            )
            self.win[i].Fence(self.rank)

    def __del__(self):
        for i in range(self.comm.size):
            self.win[i].Free()
            self.head[i].Free()

    def pop(self, rank, count=1):
        ret = False
        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head[rank].Lock(self.rank)
        self.tail[rank].Lock(self.rank)

        # Read the head and tail pointers.
        reqHead = self.head[rank].Rget_accumulate(self.minus, head, self.rank, op=MPI.SUM)
        reqTail = self.tail[rank].Rget_accumulate(self.minus, tail, self.rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()
        # print("InPop", head[0], tail[0])
        if head[0] > tail[0]:
            ret = True
            index = (head[0] - 1) % self.length

            self.win[rank].Lock(self.rank)
            self.win[rank].Get_accumulate(
                self.buff,
                self.buff,
                self.rank,
                target=[index, self.dataSize],
                op=MPI.NO_OP,
            )
            self.win[rank].Unlock(self.rank)

        else:
            self.head[rank].Accumulate(
                self.plus, self.rank, op=MPI.SUM
            )

        self.tail[rank].Unlock(self.rank)
        self.head[rank].Unlock(self.rank)

        if ret:
            return MPI.pickle.loads(self.buff)
        return None

    def push(self, data, rank=None):
        if rank is None:
            rank = self.comm.rank
        toSend = MPI.pickle.dumps(data)
        assert len(toSend) == self.dataSize

        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head[rank].Lock(self.rank)
        self.tail[rank].Lock(self.rank)

        # Read the head and tail pointers.
        reqHead = self.head[rank].Rget_accumulate(self.plus, head, self.rank, op=MPI.SUM)
        reqTail = self.tail[rank].Rget_accumulate(self.plus, tail, self.rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()

        # This is if we are going to loose data because we exceded capacity
        write = True
        if tail[0] + self.length == head[0]:
            if self.failPush:
                write = False
                self.head[rank].Accumulate(
                    self.minus, self.rank, op=MPI.SUM
                )
            else:
                self.tail[rank].Accumulate(
                    self.plus, self.rank, op=MPI.SUM
                )
            lost = 1
            capacity = self.length
        else:
            lost = 0
            capacity = head[0] - tail[0] + 1

        if write:
            # Actual write data
            index = head[0] % self.length
            self.win[rank].Lock(self.rank)
            self.win[rank].Accumulate(
                toSend, self.rank, target=[index, self.dataSize], op=MPI.REPLACE
            )
            self.win[rank].Unlock(self.rank)
            
        self.tail[rank].Unlock(self.rank)
        self.head[rank].Unlock(self.rank)
        return capacity, lost

class ExaMPICentralizedQueue(ExaData):
    def __init__(self, comm, rank=None, size=None, data=None, length=32, max_model_lag=None, failPush=False):
        self.comm = comm
        self.rank = rank
        self.length = length
        # This lets us fail a push when at full capacity
        # Otherwise will overwrite the oldest data
        self.failPush = failPush

        if data is not None:
            dataBytes = MPI.pickle.dumps(data)
            size = len(dataBytes)
        super().__init__(bytes, size, comm_size=comm.size, max_model_lag=max_model_lag)
        self.buff = bytearray(self.dataSize)
        self.plus = np.array([1], dtype=np.int64)
        self.minus = np.array([-1], dtype=np.int64)

        totalSize = 0
        headSize = 0
        tailSize = 0
        if comm.rank == rank:
            totalSize = size * self.length
            headSize = MPI.INT64_T.Get_size()
            tailSize = MPI.INT64_T.Get_size()

        self.head = []
        self.tail = []
        self.win = []
        for i in range(comm.size):
            # Setup head window
            self.head.append(MPI.Win.Allocate(headSize, comm=self.comm.raw()))
            self.head[i].Lock(self.rank)
            self.head[i].Accumulate(
                np.zeros(1, dtype=np.int64), self.rank, op=MPI.REPLACE
            )
            self.head[i].Unlock(self.rank)
            self.head[i].Fence(self.rank)

            # Setup tail window
            self.tail.append(MPI.Win.Allocate(tailSize, comm=self.comm.raw()))
            self.tail[i].Lock(self.rank)
            self.tail[i].Accumulate(
                np.zeros(1, dtype=np.int64), self.rank, op=MPI.REPLACE
            )
            self.tail[i].Unlock(self.rank)
            self.tail[i].Fence(self.rank)

            # Setup data window
            self.win.append(
                MPI.Win.Allocate(totalSize, disp_unit=size, comm=self.comm.raw())
            )
            self.win[i].Fence(self.rank)

    def __del__(self):
        for i in range(self.comm.size):
            self.win[i].Free()
            self.head[i].Free()

    def pop(self, rank, count=1):
        ret = True
        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)
        rank = int(rank)

        self.head[rank].Lock(self.rank)
        self.tail[rank].Lock(self.rank)

        # Read the head and tail pointers.
        reqHead = self.head[rank].Rget_accumulate(self.minus, head, self.rank, op=MPI.NO_OP)
        reqTail = self.tail[rank].Rget_accumulate(self.plus, tail, self.rank, op=MPI.SUM)
        reqHead.wait()
        reqTail.wait()

        # Is there space
        if head[0] > tail[0]:
            index = tail[0] % self.length
            self.win[rank].Lock(self.rank)
            self.win[rank].Get_accumulate(
                self.buff,
                self.buff,
                self.rank,
                target=[index, self.dataSize],
                op=MPI.NO_OP,
            )
            self.win[rank].Unlock(self.rank)
        else:
            # Dec the tail pointer
            self.tail[rank].Accumulate(self.minus, self.rank, op=MPI.SUM)
            ret = False

        self.tail[rank].Unlock(self.rank)
        self.head[rank].Unlock(self.rank)

        if ret:
            return MPI.pickle.loads(self.buff)
        return None

    def push(self, data, rank=None):
        if rank is None:
            rank = self.comm.rank
        toSend = MPI.pickle.dumps(data)
        assert len(toSend) <= self.dataSize

        head = np.zeros(1, dtype=np.int64)
        tail = np.zeros(1, dtype=np.int64)

        self.head[rank].Lock(self.rank)
        self.tail[rank].Lock(self.rank)

        reqHead = self.head[rank].Rget_accumulate(self.plus, head, self.rank, op=MPI.SUM)
        reqTail = self.tail[rank].Rget_accumulate(self.plus, tail, self.rank, op=MPI.NO_OP)
        reqHead.wait()
        reqTail.wait()

        write = True
        headIndex = head[0] % self.length
        tailIndex = tail[0] % self.length
        if head[0] > tail[0] and headIndex == tailIndex:
            if self.failPush:
                write = False
                self.head[rank].Accumulate(
                    self.minus, self.rank, op=MPI.SUM
                )
            else:
                self.tail[rank].Accumulate(
                    self.plus, self.rank, op=MPI.SUM
                )
            lost = 1
            capacity = self.length
        else:
            lost = 0
            capacity = head[0] - tail[0]

        if write:    
            self.win[rank].Lock(self.rank)
            self.win[rank].Accumulate(
                toSend, self.rank, target=[headIndex, len(toSend)], op=MPI.REPLACE
            )
            self.win[rank].Unlock(self.rank)

        self.tail[rank].Unlock(self.rank)
        self.head[rank].Unlock(self.rank)
        
        return capacity, lost

