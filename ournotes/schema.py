from pathlib import Path

from google.protobuf import descriptor_pool, descriptor_pb2, empty_pb2, message_factory, json_format


class Schema:
    """Typed gRPC messages recovered from the APK, kept outside the Git tree."""

    def __init__(self, path):
        self.path = Path(path)
        self.files = descriptor_pb2.FileDescriptorSet.FromString(self.path.read_bytes()).file
        self.pool = descriptor_pool.DescriptorPool()
        self.pool.AddSerializedFile(descriptor_pb2.DESCRIPTOR.serialized_pb)
        self.pool.AddSerializedFile(empty_pb2.DESCRIPTOR.serialized_pb)
        pending = {f.name: f for f in self.files if f.name not in ('google/protobuf/descriptor.proto', 'google/protobuf/empty.proto')}
        loaded = {'google/protobuf/descriptor.proto', 'google/protobuf/empty.proto'}
        while pending:
            ready = [name for name, f in pending.items() if set(f.dependency) <= loaded]
            if not ready:
                raise ValueError('Unresolved protobuf dependencies: ' + ', '.join(sorted(pending)))
            for name in ready:
                self.pool.AddSerializedFile(pending.pop(name).SerializeToString())
                loaded.add(name)
        self.methods = {}
        for f in self.files:
            for service in f.service:
                full = f'{f.package}.{service.name}' if f.package else service.name
                descriptor = self.pool.FindServiceByName(full)
                for method in descriptor.methods:
                    self.methods[f'/{full}/{method.name}'] = method

    def method(self, path):
        try:
            method = self.methods[path]
        except KeyError:
            raise ValueError(f'RPC absent from APK schema: {path}') from None
        if method.client_streaming or method.server_streaming:
            raise ValueError('Only unary RPCs are supported')
        return method

    def request(self, path, data):
        method = self.method(path)
        message = message_factory.GetMessageClass(method.input_type)()
        return json_format.ParseDict(data, message, ignore_unknown_fields=False)

    def response(self, path, data):
        method = self.method(path)
        message = message_factory.GetMessageClass(method.output_type).FromString(data)
        return json_format.MessageToDict(message, preserving_proto_field_name=True)

    def describe(self, path):
        method = self.method(path)
        def fields(descriptor):
            return [{'name': f.name, 'number': f.number, 'type': f.message_type.full_name if f.message_type else f.type,
                     'repeated': f.is_repeated} for f in descriptor.fields]
        return {'method': path, 'request': fields(method.input_type), 'response': fields(method.output_type)}
