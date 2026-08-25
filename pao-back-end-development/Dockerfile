# Stage 1: Build the Go binary
FROM golang:latest AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -a -installsuffix cgo -o main .

# Stage 2: Create the final image
FROM alpine:latest

# Install necessary packages including tzdata for timezone handling
RUN apk --no-cache add ca-certificates tzdata

# Set the timezone environment variable
ENV TZ=Asia/Kolkata

# Create a non-root user and set the working directory
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser
WORKDIR /home/appuser/

# Copy the Go binary and configuration file from the builder stage
COPY --from=builder /app/main .
COPY --from=builder /app/config.yaml .

EXPOSE 8080
CMD ["sh", "-c", "until wget -qO- http://localhost:15021/healthz/ready; do sleep 5; done && ./main"]
#CMD ["./main"]