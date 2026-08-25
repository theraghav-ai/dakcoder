package routes

import (
	//"github.com/gin-gonic/gin"
	_ "gotemplate/docs"

	r "gitlab.cept.gov.in/it-2.0-common/api-server"

	handler "gotemplate/handler"
	"net/http"
	"sync/atomic"

	"github.com/gin-gonic/gin"
	swaggerFiles "github.com/swaggo/files"
	ginSwagger "github.com/swaggo/gin-swagger"
)

var isShuttingDown atomic.Value

func init() {
	isShuttingDown.Store(false)
}

// SetIsShuttingDown is an exported function that allows other packages to update the isShuttingDown value
// func SetIsShuttingDown(shuttingDown bool) {
// 	isShuttingDown.Store(shuttingDown)
// }

func HealthCheckHandler(c *gin.Context) {
	shuttingDown := isShuttingDown.Load().(bool)
	if shuttingDown {
		// If the server is shutting down, respond with Service Unavailable
		c.JSON(http.StatusServiceUnavailable, gin.H{"status": "unhealthy"})
		return
	}
	// If the server is not shutting down, respond with OK
	c.JSON(http.StatusOK, gin.H{"status": "healthy"})
}
func Routes(router *r.Router,
	paogenHandler *handler.PaogenHandler,
	transferentryHandler *handler.TransferEntryHandler,
	publicacctHandler *handler.PublicAcctHandler,
	objectionHandler *handler.ObjectionHandler,
	fileHandler *handler.FileHandler) {
	// ecmsFileHandler *handler.ECMSFileHandler

	router.GET("/healthz", HealthCheckHandler)
	//add subroutes.
	v1 := router.Group("/v1")
	{ // @Router /users
		router.GET("/swagger/*any", ginSwagger.WrapHandler(swaggerFiles.Handler))

		paogen := v1.Group("/pao-gen")
		{

			paogen.GET("/hoa-only", paogenHandler.ListHoaHandler)
			paogen.GET("/search-account-codes", paogenHandler.ListAccountCodeHandler)
			paogen.PUT("/pfms-updation", paogenHandler.UpdatePfmsHandler)
			paogen.GET("/office-names/:id", paogenHandler.FetchOfficenameHandler)                                    // 1
			paogen.GET("/so-office-details/:id", paogenHandler.FetchSOOfficeDetailsHandler)                          // 60
			paogen.POST("/pfms-submission", paogenHandler.FetchPfmsHandler)                                          // 22
			paogen.GET("/pfms-submission-pending-list/:fin-year", paogenHandler.ListPfmsXmlSubmissionPendingHandler) //51
			paogen.POST("/ddo-master", paogenHandler.CreateDdomasterHandler)                                         // 52
			paogen.PUT("/ddo-master", paogenHandler.FetchOfficenameHandler)                                          // 53
			paogen.DELETE("/ddo-master/:id", paogenHandler.FetchOfficenameHandler)                                   // 54
			paogen.PUT("/pfms-submission/:unique-identifier", paogenHandler.UpdatePfmsSubmissionStatusHandler)       // 55
			paogen.POST("/pfms-resubmission/:unique-identifier", paogenHandler.FetchPfmsReSubmissionHandler)         // 63 //cashbook resubmission

			paos := paogen.Group("/pao")
			{
				paos.GET("", paogenHandler.ListPAOHandler)                                                 // 2
				paos.GET("/inter-pao", paogenHandler.ListInterPAOTEHandler)                                // 56
				paos.GET("/:pao-code/ddos", paogenHandler.ListDDOHandler)                                  // 3
				paos.GET("/:pao-code/cashbook/ddo-lists", paogenHandler.ListDDOPFMSHandler)                // 5
				paos.PUT("/:pao-code/cashbook/ddo-lists", paogenHandler.UpdateDDOCashbookListHandler)      // 4
				paos.GET("/:pao-code/cashbook/verification-pending", paogenHandler.ListPfmsPendingHandler) // 8
				paos.GET("/:pao-code/cashaccount/ddo-lists", paogenHandler.ListDdoPfmsMonthlyHandler)
				paos.GET("/office/:office_id/cashaccount/ddo-lists", paogenHandler.ListDdoPfmsMonthlyOffHandler)                          // 10
				paos.PUT("/:pao-code/cashaccount/ddo-lists", paogenHandler.UpdateDdoMonthlyHandler)                                       // 9
				paos.GET("/:pao-code/pfms-submission-status", paogenHandler.ListPfmsSubmissionStatusHandler)                              // 24
				paos.GET("/:pao-code/te-pfms-submission-status", paogenHandler.ListPfmsTESubmissionStatusHandler)                         // 25
				paos.GET("/:pao-code/prao/accounts", paogenHandler.FetchPraoAccountHandler)                                               // 26
				paos.GET("/:pao-code/prao/account-submission-status", paogenHandler.FetchPraoAccountSubStatusHandler)                     // 28
				paos.GET("/:pao-code/transfer-entry/reports", transferentryHandler.ListTransferEntryReportHandler)                        // 17
				paos.GET("/:pao-code/transfer-entry/sub-accounts/pao-reports", transferentryHandler.ListPaoSubTransferEntryReportHandler) // 18 check

				paos.GET("/:pao-code/cashaccount/consacc", paogenHandler.GetConsolidatedCashAccountHandler)

			}

			{
				ddos := paogen.Group("/ddo")
				ddos.GET("/:ddo-code/cashbook/ddo-details", paogenHandler.FetchDDOCashbookHandler)                                 // 6
				ddos.GET("/:ddo-code/cashaccount/ddo-details", paogenHandler.FetchDdoMonthlyDetailHandler)                         // 11
				ddos.GET("/:ddo-code/transfer-entry/sub-accounts/reports", transferentryHandler.ListDdoTransferEntryReportHandler) // 19 check
				ddos.GET("/:ddo-code/public-acct/broad-sheet", publicacctHandler.ListBroadsheetHandler)                            // 30
			}

			cashbook := paogen.Group("/cashbook")
			{
				cashbook.POST("/verifications", paogenHandler.CreatePFMSVerificationHandler)            // 7
				cashbook.POST("/verifications-empty", paogenHandler.CreateEmptyPFMSVerificationHandler) // 64
				cashbook.GET("/pfms-status", paogenHandler.GetCashbookPfmsStatusHandler)                //
				cashbook.POST("/reversion-resubmission", paogenHandler.RevertResubmitCashbookHandler)   // as of now not used
				cashbook.POST("/reversion", paogenHandler.RevertCashbookHandler)                        // as of now not used
				cashbook.GET("/reversion-list", paogenHandler.GetCashbookReversionListHandler)          //
				cashbook.POST("/reversion-prao", paogenHandler.RevertCashbookPostHandler)               //
				cashbook.GET("/reversion-records", paogenHandler.GetReversionRecordsHandler)            //only where reversion is done at PFMS.
				cashbook.POST("/post-negative-entry", paogenHandler.PostNegativeEntryHandler)
				cashbook.GET("/reversion-pending", paogenHandler.GetReversionPendingHandler)
				cashbook.POST("/test-post-negative-entry", paogenHandler.TestPostNegativeEntryHandler) // just a test entry
				cashbook.GET("/all-reversion-records", paogenHandler.GetAllReversionRecordsHandler)    //for review of reversions.

			}

			cashaccount := paogen.Group("/cashaccount")
			{
				cashaccount.POST("/verifications", paogenHandler.CreatePfmsMonthlyVerifiedHandler) // 12
				cashaccount.POST("/reversion-cashacc", paogenHandler.RevertCashAccountPostHandler)
				cashaccount.POST("/cashacc-reversion", paogenHandler.CashAccountReversionHandler) //new
				cashaccount.GET("/ddo/cashacc-status/:ddo-code", paogenHandler.ListDdoPfmsStatusHandler)
				cashaccount.GET("/prao-status", paogenHandler.GetPaoPraoStatusHandler)

			}

			prao := paogen.Group("/prao")
			{
				prao.POST("/account-submission", paogenHandler.CreatePraoAccountHandler)                            // 27
				prao.GET("/:prao-office-id/account-submission-list", paogenHandler.ListPraoAccountSubStatusHandler) // 29
			}

		}

		transferentry := v1.Group("/transfer-entry")
		{
			transferentry.POST("", transferentryHandler.CreateTransferEntryHandler)                                         // 13
			transferentry.POST("/inter-pao", transferentryHandler.CreateTransferEntryInterPaoHandler)                       // 56
			transferentry.GET("/inter-pao/master", transferentryHandler.ListTransferEntryInterPaoMasterHandler)             // 57
			transferentry.GET("/inter-pao", transferentryHandler.ListTransferEntryInterPaoHandler)                          // 58
			transferentry.GET("/inter-pao/details/:trans-id", transferentryHandler.FetchInterPaoTransferentryDetailHandler) // 59
			transferentry.PUT("/inter-pao/master", transferentryHandler.UpdateInterPaoTransferEntryVerifyMasterHandler)     // 61
			transferentry.PUT("/inter-pao", transferentryHandler.UpdateInterPaoTransferEntryVerifyHandler)                  // 62
			transferentry.POST("/direct", transferentryHandler.CreateTransferEntryDirectHandler)                            // 14
			transferentry.POST("/direct-brs", transferentryHandler.CreateTransferEntryDirectBRSHandler)                     //
			transferentry.PUT("/:transfer-entry-id/rejection", transferentryHandler.UpdateTransferEntryRejectHandler)       // 15
			transferentry.PUT("/bulk-verification", transferentryHandler.UpdateTransferEntryVerifyHandler)                  // 16 //general one on one vfn
			transferentry.POST("/pfms-submission", transferentryHandler.CreatePfmsTeHandler)                                // 23
			transferentry.PUT("/pfms-reset/:pfms-unique-id", transferentryHandler.ResetPFMSFlagHandler)
			transferentry.GET("/pfms-te-reversible", transferentryHandler.GetReversiblePfmsTeHandler)
			transferentry.POST("/pfms-negative-submission", transferentryHandler.CreateNegativePfmsTeHandler)

			subaccounts := transferentry.Group("/sub-accounts")
			{
				subaccounts.POST("/verification", transferentryHandler.CreateSubaccountsTeVerifiedTempoHandler)   // 21 check
				subaccounts.GET("/details/:trans-id", transferentryHandler.FetchPaoSubTransferentryDetailHandler) // 20 check
				subaccounts.GET("/workflow/status/:workflow_id", transferentryHandler.GetWorkflowStatus)          // 47
			}

		}

		publicacct := v1.Group("/public-acct")
		{

			publicacct.GET("/appr-acct-one", publicacctHandler.ListApprAcctsHandler)              // 31
			publicacct.GET("/appr-acct-two", publicacctHandler.ListApprAcctsTwoHandler)           // 32
			publicacct.GET("/appr-acct-three", publicacctHandler.ListApprAcctsThreeHandler)       //50
			publicacct.POST("/remuneration", publicacctHandler.CreateRemunerationRateHandler)     // 33
			publicacct.PUT("/bulk-remuneration", publicacctHandler.UpdateRemunerationRateHandler) // 35
			publicacct.GET("/remuneration", publicacctHandler.ListRemunerationRateDetailHandler)  // 34
			publicacct.POST("/remuneration-calculation", publicacctHandler.RemunerationCalculation)
			publicacct.GET("/remuneration-calculated-year/:financial-year", publicacctHandler.ListRemunerationCalculatedYearDetailHandler)
			//publicacct.GET("/remuneration-calculated-item", publicacctHandler.ListRemunerationRateDetailHandler)
		}

		objection := v1.Group("/objection")
		{
			objection.POST("", objectionHandler.CreateObjectionHandler) // 36
			objection.GET("/pao/code", objectionHandler.ListObjectionCodeHandler)
			objection.GET("/pao/report", objectionHandler.ListObjectionPaoReportHandler)            // 40
			objection.GET("/:objection-id/details", objectionHandler.FetchObjectionByIdHandler)     // 39
			objection.PUT("/:objection-id/remarks", objectionHandler.UpdateObjectionHandler)        // 37
			objection.PUT("/:objection-id/closure", objectionHandler.UpdateObjectionClosureHandler) // 38
			objection.POST("/prao", objectionHandler.CreateObjectionPraoHandler)                    // 41
			objection.GET("/prao/code", objectionHandler.ListObjectionPraoCodeHandler)
			objection.GET("/prao/report", objectionHandler.ListObjectionPraoReportHandler)                   // 45
			objection.GET("/:objection-id/prao/details", objectionHandler.FetchObjectionPraoByIdHandler)     // 44
			objection.PUT("/:objection-id/prao/remarks", objectionHandler.UpdateObjectionPraoHandler)        // 42
			objection.PUT("/:objection-id/prao/closure", objectionHandler.UpdateObjectionClosurePraoHandler) // 43
		}

		objectionfile := v1.Group("/objection-file")
		{
			objectionfile.POST("/upload", fileHandler.UploadFileHandler) // 46
			objectionfile.GET("/download", fileHandler.FetchFileHandler) // 47
		}

		// objfileecms := v1.Group("/objection-file")
		// {
		// 	objfileecms.POST("/v1/ecms/objection-file/upload", ecmsFileHandler.UploadObjectionFileHandler)
		// 	objfileecms.GET("/v1/ecms/objection-file/download", ecmsFileHandler.FetchECMSObjectionFileHandler)
		// }

		// workflowfile := v1.Group("/workflow-filestatus")
		// {

		// }
	}

}
