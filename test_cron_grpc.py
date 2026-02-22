import grpc
import sys
import os

# add brain to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'packages/brain'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'packages/shared/proto'))

import brain_pb2
import brain_pb2_grpc

def run():
    channel = grpc.insecure_channel('localhost:50051')
    stub = brain_pb2_grpc.BrainServiceStub(channel)
    try:
        req = brain_pb2.ParseCronJobRequest(
            workspace_id='test',
            prompt='Remind me to check emails every weekday morning'
        )
        response = stub.ParseCronJob(req)
        print("Success:", response)
    except grpc.RpcError as e:
        print("RPC Failed:")
        print("Status Code:", e.code())
        print("Details:", e.details())

if __name__ == '__main__':
    run()
