package mcpserver

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/convert"
	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// ConvertInput is the argument shape for convert_handlers.
type ConvertInput struct {
	Root    string   `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	Paths   []string `json:"paths,omitempty" jsonschema:"handler files to convert; omit for every handler file"`
	Methods []string `json:"methods,omitempty" jsonschema:"only these handler methods"`
	DryRun  bool     `json:"dry_run,omitempty" jsonschema:"report what would change without writing"`
}

// ConvertOutput is the conversion's result with its rendered report.
type ConvertOutput struct {
	convert.Result
	Report string `json:"report"`
}

func addConvertTool(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "convert_handlers",
		Description: "Convert legacy gin handlers to the template shape by rule, and repository " +
			"methods to context.Context. A method no rule fits is left unchanged, with the reason.",
	}, convertHandler(defaultRoot))
}

func convertHandler(defaultRoot string) mcp.ToolHandlerFor[ConvertInput, ConvertOutput] {
	return func(_ context.Context, _ *mcp.CallToolRequest, in ConvertInput) (*mcp.CallToolResult, ConvertOutput, error) {
		root, err := rootFor(defaultRoot, in.Root)
		if err != nil {
			return nil, ConvertOutput{}, err
		}
		ws, err := workspace.Load(root)
		if err != nil {
			return nil, ConvertOutput{}, fmt.Errorf("load workspace: %w", err)
		}
		res, err := convert.Run(ws, convert.Options{Paths: in.Paths, Methods: in.Methods, Write: !in.DryRun})
		if err != nil {
			return nil, ConvertOutput{}, err
		}
		return nil, ConvertOutput{Result: *res, Report: convert.Render(res)}, nil
	}
}

// ConvertStepInput is the argument shape for convert_service and convert_dtos.
type ConvertStepInput struct {
	Root   string `json:"root,omitempty" jsonschema:"workspace root; omit to use the server's default"`
	DryRun bool   `json:"dry_run,omitempty" jsonschema:"report what would change without writing"`
}

// ConvertServiceOutput is the service step's result with its report.
type ConvertServiceOutput struct {
	convert.ServiceResult
	Report string `json:"report"`
}

// ConvertDTOsOutput is the DTO step's result with its report.
type ConvertDTOsOutput struct {
	convert.DTOResult
	Report string `json:"report"`
}

func addConvertStepTools(s *mcp.Server, defaultRoot string) {
	mcp.AddTool(s, &mcp.Tool{
		Name: "convert_service",
		Description: "Move routes.go into each handler's Routes(), register handlers as controllers in " +
			"bootstrap, retire routes.go, swap the legacy libraries and rebuild the test harness.",
	}, func(_ context.Context, _ *mcp.CallToolRequest, in ConvertStepInput) (*mcp.CallToolResult, ConvertServiceOutput, error) {
		ws, err := loadRoot(defaultRoot, in.Root)
		if err != nil {
			return nil, ConvertServiceOutput{}, err
		}
		res, err := convert.Service(ws, !in.DryRun)
		if err != nil {
			return nil, ConvertServiceOutput{}, err
		}
		return nil, ConvertServiceOutput{ServiceResult: *res, Report: convert.RenderService(res)}, nil
	})
	mcp.AddTool(s, &mcp.Tool{
		Name: "convert_dtos",
		Description: "Move request DTOs into handler/request/request.go and give each a Validate(): govalid " +
			"where its tags allow, the legacy validator where custom tags need it.",
	}, func(_ context.Context, _ *mcp.CallToolRequest, in ConvertStepInput) (*mcp.CallToolResult, ConvertDTOsOutput, error) {
		ws, err := loadRoot(defaultRoot, in.Root)
		if err != nil {
			return nil, ConvertDTOsOutput{}, err
		}
		res, err := convert.DTOs(ws, !in.DryRun)
		if err != nil {
			return nil, ConvertDTOsOutput{}, err
		}
		return nil, ConvertDTOsOutput{DTOResult: *res, Report: convert.RenderDTOs(res)}, nil
	})
}

func loadRoot(defaultRoot, requested string) (*workspace.Workspace, error) {
	root, err := rootFor(defaultRoot, requested)
	if err != nil {
		return nil, err
	}
	ws, err := workspace.Load(root)
	if err != nil {
		return nil, fmt.Errorf("load workspace: %w", err)
	}
	return ws, nil
}
