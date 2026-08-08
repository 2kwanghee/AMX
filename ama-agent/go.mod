module github.com/2kwanghee/AMX/ama-agent

go 1.24

require (
	github.com/2kwanghee/AMX/contracts/gen/go v0.0.0-00010101000000-000000000000
	github.com/fsnotify/fsnotify v1.7.0
	google.golang.org/grpc v1.71.0
	google.golang.org/protobuf v1.36.6
)

require (
	golang.org/x/net v0.34.0 // indirect
	golang.org/x/sys v0.29.0 // indirect
	golang.org/x/text v0.21.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20250115164207-1a7da9e5054f // indirect
)

// Local, unpublished contract module (D6). Path matches contracts/gen/go/go.mod.
replace github.com/2kwanghee/AMX/contracts/gen/go => ../contracts/gen/go
