# Makefile
YOUR_SECRET_KEY=yourActualSecretKeyHere
# Default Go linker flags
LDFLAGS=-ldflags "-s -w"
# Default Go binary name
BINARY=GO_FRAME

# Name of the packaged artifact
PACKAGE=$(BINARY).tar.gz

GO_BINARY=/usr/bin/go/bin/go

# Default make command
all: deps test build package

# Install the necessary dependencies
dependencies:
	@echo "Fetching dependencies..."
	
	@$(GO_BINARY) mod download

	@echo "Dependencies fetched successfully."




# Build the binary
# Example
# When you run make, you'd provide the required parameters:
# make ciBuildVersion=1.0.0 ciBuildBranch=master ciBuildEnv=prod ciGitHash=abcdef123456 ciBuildTimestamp="2023-09-12 12:34:56"
build:
	@echo "Building the binary..."
	@$(GO_BINARY) build $(LDFLAGS) -o $(BINARY) *.go
	@echo "Binary built successfully."

# Package the binary into a tar.gz
package:
	@echo "Packaging the binary..."
	@tar czvf $(PACKAGE) $(BINARY)
	@echo "Binary packaged successfully."

# Run the tests
test:
	@echo "Running tests..."
	@$(GO_BINARY) test -v ./...
	@echo "Tests completed."

# Clean up
clean:
	@echo "Cleaning up..."
	@$(GO_BINARY) clean
	@rm -f $(BINARY)
	@rm -f $(PACKAGE)
	@echo "Cleaned up."

# Helper commands
.PHONY: all deps build test clean package
